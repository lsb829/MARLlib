"""
Implement HAPPO algorithm based on Rlib original PPO.
__author__: minquan
__data__: March-29-2022
"""

import logging
import random
from typing import Dict, List, Type, Union, Tuple
from ray.rllib.models.torch.torch_action_dist import TorchDistributionWrapper
from ray.rllib.policy.policy import Policy
from ray.rllib.models.modelv2 import ModelV2
from ray.rllib.utils.torch_ops import explained_variance, sequence_mask
import numpy as np
from ray.rllib.evaluation.postprocessing import Postprocessing, compute_gae_for_sample_batch
from ray.rllib.policy.sample_batch import SampleBatch
from ray.rllib.utils.framework import try_import_tf, try_import_torch
from ray.rllib.utils.typing import TrainerConfigDict, TensorType, \
    LocalOptimizer
from ray.rllib.agents.ppo.ppo import PPOTrainer, DEFAULT_CONFIG as PPO_CONFIG
from ray.rllib.agents.ppo.ppo_torch_policy import PPOTorchPolicy, ValueNetworkMixin, KLCoeffMixin
from ray.rllib.utils.torch_ops import apply_grad_clipping
from ray.rllib.policy.torch_policy import LearningRateSchedule, EntropyCoeffSchedule
from marl.algos.utils.setup_utils import setup_torch_mixins, get_agent_num
from marl.algos.utils.centralized_critic_hetero import (
    get_global_name,
    STATE,
    add_all_agents_gae,
    value_normalizer,
)
from ray.rllib.examples.centralized_critic import CentralizedValueMixin
from marl.algos.utils.setup_utils import get_device
from marl.algos.utils.manipulate_tensor import flat_grad, flat_params
from torch.nn.utils import parameters_to_vector, vector_to_parameters
from icecream import ic

tf1, tf, tfv = try_import_tf()
torch, nn = try_import_torch()

logger = logging.getLogger(__name__)


def happo_surrogate_loss(
        policy: Policy, model: ModelV2,
        dist_class: Type[TorchDistributionWrapper],
        train_batch: SampleBatch) -> Union[TensorType, List[TensorType]]:
    """Constructs the loss for Proximal Policy Objective.
    Args:
        policy (Policy): The Policy to calculate the loss for.
        model (ModelV2): The Model to calculate the loss for.
        dist_class (Type[ActionDistribution]: The action distr. class.
        train_batch (SampleBatch): The training data.
    Returns:
        Union[TensorType, List[TensorType]]: A single loss tensor or a list
            of loss tensors.
    """

    # CentralizedValueMixin.__init__(policy)

    logits, state = model(train_batch)
    curr_action_dist = dist_class(logits, model)

    # RNN case: Mask away 0-padded chunks at end of time axis.
    if state:
        B = len(train_batch[SampleBatch.SEQ_LENS])
        max_seq_len = logits.shape[0] // B
        mask = sequence_mask(
            train_batch[SampleBatch.SEQ_LENS],
            max_seq_len,
            time_major=model.is_time_major())
        mask = torch.reshape(mask, [-1])
        num_valid = torch.sum(mask)

        def reduce_mean_valid(t):
            return torch.sum(t[mask]) / num_valid

    # non-RNN case: No masking.
    else:
        mask = None
        reduce_mean_valid = torch.mean

    # vf_saved = model.value_function

    g_action_key = get_global_name(SampleBatch.ACTIONS)

    opp_action_in_cc = policy.config["model"]["custom_model_config"]["opp_action_in_cc"] and g_action_key in train_batch

    if opp_action_in_cc in train_batch:
        global_actions = train_batch[g_action_key]
    else:
        global_actions = None

    # model.value_function = lambda: policy.model.central_value_function(
    #     train_batch[STATE],
    #     global_actions
    # )

    if g_action_key in train_batch: # if global action key in train batch, the other info must not be empty.
        sub_losses = []
        m_advantage = train_batch[Postprocessing.ADVANTAGES]

        agents_num = get_agent_num(policy)

        random_indices = np.random.permutation(range(agents_num))

        # in order to get each agent's information, if random_indices is len(agents_num) - 1, we set
        # this as our current_agent, and get the information from generally train batch.
        # otherwise, we get the agent information from "GLOBAL_LOGITS", "GLOBAL_ACTIONS", etc

        def is_current_agent(i): return i == (agents_num - 1)

        # torch.autograd.set_detect_anomaly(True)

        # ic([id(p) for p in model.other_policies])
        # ic(len(model.other_policies))
        # ic(agents_num)

        all_policies_names = list(model.other_policies.keys()) + ['self']

        random.shuffle(all_policies_names)

        m_advantage = train_batch[Postprocessing.ADVANTAGES]

        for policy_name in all_policies_names:
            # ic(policy_name)
            if policy_name == 'self':
                agent_policy = policy
                old_action_log = train_batch[SampleBatch.ACTION_LOGP]
                # curr_action_dist = dist_class(logits, model)
            else:
                agent_policy = model.other_policies[policy_name]
                old_action_log = train_batch[get_global_name(SampleBatch.ACTION_LOGP, policy_name)]

            _p_model = agent_policy.model

            _p_model.train()
            logits, state = _p_model(train_batch)
            curr_action_dist = dist_class(logits, _p_model)

            step_logp_ratio = torch.exp(
                curr_action_dist.logp(train_batch[SampleBatch.ACTIONS]) -
                old_action_log)

            step_loss = torch.min(
                m_advantage * step_logp_ratio,
                m_advantage * torch.clamp(
                    step_logp_ratio, 1 - policy.config["clip_param"],
                    1 + policy.config["clip_param"])
            )

            sub_losses.append(step_loss)  # for recoding, need the real step-loss,

            torch.autograd.set_detect_anomaly(True)

            loss_grad = torch.autograd.grad(reduce_mean_valid(-1 * step_loss), _p_model.actor_parameters(), allow_unused=True, retain_graph=True)

            adam_update_part = _p_model.update_adam(flat_grad(loss_grad))

            new_parameters = (
                parameters_to_vector(_p_model.actor_parameters()) - adam_update_part * policy.cur_lr
            )

            vector_to_parameters(new_parameters, _p_model.actor_parameters())

            new_logits, _ = _p_model(train_batch)
            new_action_dist = dist_class(new_logits, _p_model)

            new_logp_ratio = torch.exp(
                new_action_dist.logp(train_batch[SampleBatch.ACTIONS]) -
                old_action_log
            )

            m_advantage = new_logp_ratio * m_advantage

        surrogate_loss = torch.mean(torch.stack(sub_losses, axis=1), axis=1)

        model_already_updated = True
    else:
        logp_ratio = torch.exp(
            curr_action_dist.logp(train_batch[SampleBatch.ACTIONS]) -
            train_batch[SampleBatch.ACTION_LOGP])

        surrogate_loss = torch.min(
            train_batch[Postprocessing.ADVANTAGES] * logp_ratio,
            train_batch[Postprocessing.ADVANTAGES] * torch.clamp(
                logp_ratio, 1 - policy.config["clip_param"],
                1 + policy.config["clip_param"]))
        model_already_updated = False

    mean_policy_loss = reduce_mean_valid(-surrogate_loss)

    prev_action_dist = dist_class(train_batch[SampleBatch.ACTION_DIST_INPUTS], model)
    action_kl = prev_action_dist.kl(curr_action_dist)
    mean_kl_loss = reduce_mean_valid(action_kl)

    curr_entropy = curr_action_dist.entropy()
    mean_entropy = reduce_mean_valid(curr_entropy)

    # Compute a value function loss.
    # if policy.model.model_config['custom_model_config']['normal_value']:
    value_normalizer.update(train_batch[Postprocessing.VALUE_TARGETS])
    train_batch[Postprocessing.VALUE_TARGETS] = value_normalizer.normalize(train_batch[Postprocessing.VALUE_TARGETS])

    if policy.config["use_critic"]:
        prev_value_fn_out = train_batch[SampleBatch.VF_PREDS] #
        value_fn_out = model.value_function()  # same as values
        vf_loss1 = torch.pow(
            value_fn_out.to(device=get_device()) - train_batch[Postprocessing.VALUE_TARGETS].to(device=get_device()), 2.0)
        vf_clipped = (prev_value_fn_out + torch.clamp(
            value_fn_out - prev_value_fn_out, -policy.config["vf_clip_param"],
            policy.config["vf_clip_param"])).to(device=get_device())
        vf_loss2 = torch.pow(
            vf_clipped.to(device=get_device()) - train_batch[Postprocessing.VALUE_TARGETS].to(device=get_device()), 2.0)
        vf_loss = torch.max(vf_loss1, vf_loss2).to(device=get_device())
        mean_vf_loss = reduce_mean_valid(vf_loss).to(device=get_device())
    # Ignore the value function.
    else:
        vf_loss = mean_vf_loss = 0.0

    # model.value_function = vf_saved
    # recovery the value function.

    total_loss = (policy.kl_coeff * action_kl.to(device=get_device()) +
                  policy.config["vf_loss_coeff"] * vf_loss.to(device=get_device()) -
                  policy.entropy_coeff * curr_entropy.to(device=get_device()))

    if not model_already_updated:
        total_loss = -surrogate_loss.to(device=get_device()) + total_loss

    total_loss = reduce_mean_valid(total_loss)

    # Store values for stats function in model (tower), such that for
    # multi-GPU, we do not override them during the parallel loss phase.
    model.tower_stats["total_loss"] = total_loss
    model.tower_stats["mean_policy_loss"] = mean_policy_loss
    model.tower_stats["mean_vf_loss"] = mean_vf_loss
    model.tower_stats["vf_explained_var"] = explained_variance(
        train_batch[Postprocessing.VALUE_TARGETS].to(device=get_device()),
        model.value_function().to(device=get_device()))
    model.tower_stats["mean_entropy"] = mean_entropy
    model.tower_stats["mean_kl_loss"] = mean_kl_loss

    return total_loss


def make_happo_optimizers(policy: Policy,
                          config: TrainerConfigDict) -> Tuple[LocalOptimizer]:
    """Create separate optimizers for actor & critic losses."""

    # Set epsilons to match tf.keras.optimizers.Adam's epsilon default.
    policy._actor_optimizer = torch.optim.Adam(
        params=policy.model.policy_variables(),
        lr=config["actor_lr"],
        eps=1e-7)

    policy._critic_optimizer = torch.optim.Adam(
        params=policy.model.critic_variables(), lr=config["critic_lr"], eps=1e-5)

    # Return them in the same order as the respective loss terms are returned.
    return policy._actor_optimizer, policy._critic_optimizer


HAPPOTorchPolicy = lambda ppo_with_critic: PPOTorchPolicy.with_updates(
    name="HAPPOTorchPolicy",
    get_default_config=lambda: ppo_with_critic,
    postprocess_fn=add_all_agents_gae,
    loss_fn=happo_surrogate_loss,
    before_init=setup_torch_mixins,
    extra_grad_process_fn=apply_grad_clipping,
    mixins=[
        LearningRateSchedule, EntropyCoeffSchedule, KLCoeffMixin,
        CentralizedValueMixin
    ])


def get_policy_class_happo(ppo_with_critic):
    def __inner(config_):
        if config_["framework"] == "torch":
            return HAPPOTorchPolicy(ppo_with_critic)
    return __inner


HAPPOTrainer = lambda ppo_with_critic: PPOTrainer.with_updates(
    name="HAPPOTrainer",
    default_policy=None,
    get_policy_class=get_policy_class_happo(ppo_with_critic),
)
