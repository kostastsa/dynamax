from functools import partial
import chex
import jax
from jax import vmap, lax, jit
from jax import numpy as jnp
from ssm_jax.bp.gauss_bp_utils import (
    potential_from_conditional_linear_gaussian,
    pair_cpot_condition,
    pair_cpot_marginalize,
    pair_cpot_absorb_message,
    info_multiply,
    info_divide,
)


@chex.dataclass
class GaussianChainPotentials:
    """Container class for Gaussian Chain Potentials.

      Both `latent_pots` and `obs_pots` contain the canonical parameters for a
        gaussian potential over a pair of variables.

     It is assumed that the latent and observed variables have the same shape
      along the chain (but not necessarily the same as each other). As occurs for
      instance in temporal models. This means that the potential parameters can be
      stacked as rows for and used with `jax.vmap` and `jax.lax.scan`.

    Attributes:
      prior_pot: A tuple containg the parameters for the prior potential over
                  the first latent state (K, h).

      latent_pots: A tuple containing the parameters for each pairwise latent
                    clique potential - ((K11, K12, K22),(h1, h2)). The ith row
                    of each array contains parameters for the clique containing
                    the pair of latent states at times (i, i+1).
                    Arrays have shapes:
                        K11, K12, K22 - (T-1, D_hid, D_hid)
                        h1, h2 - (T-1, D_hid)

      obs_pots: A tuple containing the parameters for each pairwise
                      emission clique potential - ((K11, K12, K22),(h1, h2)).
                      Arrays have shapes:
                         K11 - (T, D_hid, D_hid)
                         K12 - (T, D_hid, D_obs)
                         K22 - (T, D_obs, D_obs)
                         h1 - (T, D_hid)
                         h2 - (T, D_obs)
    """

    prior_pot: chex.Array
    latent_pots: chex.Array
    obs_pots: chex.Array


def gauss_chain_potentials_from_lgssm(lgssm_params, inputs, T=None):
    """Construct pairwise latent and emission clique potentials from model.

    Args:
        lgssm_params: an LGSSMInfoParams instance.
        inputs (T,D_in): array of inputs.
        T (int): number of timesteps to to unroll the lgssm, only used if
                  `inputs=None`.

    Returns:
        prior_pot: A tuple of parameters representing the prior potential,
                    (Lambda0, eta0)
    """
    if inputs is None:
        if T is not None:
            D_in = lgssm_params.dynamics_input_weights.shape[1]
            inputs = jnp.zeros((T, D_in))
        else:
            raise ValueError("One of `inputs` or `T` must not be None.")

    B, b = lgssm_params.dynamics_input_weights, lgssm_params.dynamics_bias
    D, d = lgssm_params.emission_input_weights, lgssm_params.emission_bias
    latent_net_inputs = vmap(jnp.dot, (None, 0))(B, inputs) + b
    emission_net_inputs = vmap(jnp.dot, (None, 0))(D, inputs) + d

    Lambda0, mu0 = lgssm_params.initial_precision, lgssm_params.initial_mean
    prior_pot = (Lambda0, Lambda0 @ mu0)

    F, Q_prec = lgssm_params.dynamics_matrix, lgssm_params.dynamics_precision
    latent_pots = vmap(potential_from_conditional_linear_gaussian, (None, 0, None))(F, latent_net_inputs[:-1], Q_prec)

    H, R_prec = lgssm_params.emission_matrix, lgssm_params.emission_precision
    emission_pots = vmap(potential_from_conditional_linear_gaussian, (None, 0, None))(H, emission_net_inputs, R_prec)

    gauss_chain_potentials = GaussianChainPotentials(
        prior_pot=prior_pot, latent_pots=latent_pots, obs_pots=emission_pots
    )
    return gauss_chain_potentials


def gauss_chain_bp(gauss_chain_pots, obs):
    """Belief propagation on a Gaussian chain.

    Calculate the canonical parameters for the marginal probability of latent
    states conditioned on the full set of observation,
      p(x_t | y_{1:T}).

    Args:
        gauss_chain_pots: GaussianChainPotentials object containing the prior
                           potential for the first latent state and pairwise
                           potentials for the latent and observed variables.
        obs (T,D_obs): Array containing the observations.

    Returns:
        smoothed_bels: canonical parameters of marginal distribution of each latent
                        state condition on all observations, (K_smoothed, h_smoothed) with
                        shapes,
                           K_smoothed (T, D_hid, D_hid)
                           h_smoothed (T, D_hid).
    """
    prior_pot, latent_pots, emission_pots = gauss_chain_pots.to_tuple()

    # Extract first emission  potential
    init_emission_pot = jax.tree_map(lambda a: a[0], emission_pots)
    emission_pots_rest = jax.tree_map(lambda a: a[1:], emission_pots)

    # Combine first emission message with prior
    init_emission_message = pair_cpot_condition(init_emission_pot, obs[0], obs_var=2)
    init_carry = info_multiply(prior_pot, init_emission_message)

    def _forward_step(carry, x):
        prev_bel = carry
        latent_pot, emission_pot, y = x

        # Calculate latent message
        latent_pot = pair_cpot_absorb_message(latent_pot, prev_bel, message_var=1)
        latent_message = pair_cpot_marginalize(latent_pot, marginalize_onto=2)

        # Calculate emission message
        emission_message = pair_cpot_condition(emission_pot, y, obs_var=2)

        # Combine messages
        bel = info_multiply(latent_message, emission_message)

        return bel, (bel, latent_message)

    # Message pass forwards along chain
    _, (filtered_bels, forward_messages) = lax.scan(
        _forward_step, init_carry, (latent_pots, emission_pots_rest, obs[1:])
    )
    # Append first belief
    filtered_bels = jax.tree_map(lambda h, t: jnp.row_stack((h[None, ...], t)), init_carry, filtered_bels)

    # Extract final belief
    init_carry = jax.tree_map(lambda a: a[-1], filtered_bels)
    filtered_bels_rest = jax.tree_map(lambda a: a[:-1], filtered_bels)

    def _backward_step(carry, x):
        prev_bel = carry
        bel, message_up, latent_pot = x

        # Divide out forward message
        bel_minus_message_up = info_divide(prev_bel, message_up)
        # Absorb into joint potential
        latent_pot = pair_cpot_absorb_message(latent_pot, bel_minus_message_up, message_var=2)
        message_down = pair_cpot_marginalize(latent_pot, marginalize_onto=1)

        bel = info_multiply(bel, message_down)
        return bel, bel

    # Message pass back along chain
    _, smoothed_bels = lax.scan(
        _backward_step, init_carry, (filtered_bels_rest, forward_messages, latent_pots), reverse=True
    )
    # Append final belief
    smoothed_bels = jax.tree_map(lambda h, t: jnp.row_stack((h, t[None, ...])), smoothed_bels, init_carry)

    return smoothed_bels
