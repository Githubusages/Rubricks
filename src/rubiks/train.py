import os
from typing import List

import matplotlib.pyplot as plt
plt.rcParams.update({"font.size": 22})
import numpy as np
import torch

from src.rubiks.solving.search import DeepSearcher
from src.rubiks.solving.agents import DeepAgent
from src.rubiks import cpu, gpu, no_grad, reset_cuda
from src.rubiks.cube.cube import Cube
from src.rubiks.model import Model, ModelConfig
from src.rubiks.solving.evaluation import Evaluator
from src.rubiks.utils.logger import Logger, NullLogger, unverbose
from src.rubiks.utils.ticktock import TickTock


class Train:

	states_per_rollout: int

	train_rollouts: np.ndarray
	value_losses: np.ndarray
	policy_losses: np.ndarray
	train_losses: np.ndarray
	param_changes = list()
	param_total_changes = list()
	eval_rewards = list()
	depths: np.ndarray
	avg_value_targets: List[np.ndarray] = list()

	def __init__(self,
			rollouts: int,
			batch_size: int, # Required to be > 1 when training with batchnorm
			rollout_games: int,
			rollout_depth: int,
			loss_weighting: str,
			optim_fn,
			lr: float,
			gamma: float,
			lr_reductions: int,
			agent: DeepAgent,
			evaluator: Evaluator,
			evaluations: int,
			logger: Logger		= NullLogger(),
			policy_criterion	= torch.nn.CrossEntropyLoss,
			value_criterion		= torch.nn.MSELoss,
		):

		self.rollouts = rollouts
		self.train_rollouts = np.arange(self.rollouts)
		self.batch_size = self.states_per_rollout if not batch_size else batch_size
		self.rollout_games = rollout_games
		self.rollout_depth = rollout_depth
		self.depths = np.arange(1, rollout_depth)
		self.loss_weighting = loss_weighting
		self.adi_ff_batches = 1  # Number of batches used for feedforward in ADI_traindata. Reduces vram usage

		self.evaluations = np.unique(np.linspace(0, self.rollouts-1, evaluations, dtype=int)) if evaluations else np.array([], dtype=int)
		self.evaluations.sort()
		self.agent = agent

		self.lr	= lr
		self.gamma = gamma
		self.lr_reductions = lr_reductions
		self.optim = optim_fn
		self.policy_criterion = policy_criterion(reduction='none')
		self.value_criterion = value_criterion(reduction='none')

		self.evaluator = evaluator
		self.log = logger
		self.log("\n".join([
			"Created trainer",
			f"Learning rate and gamma: {self.lr} and {self.gamma}",
			f"  Learning rate will update {self.lr_reductions} times during training: lr <- gamma * lr",
			f"Optimizer:      {self.optim}",
			f"Policy and value criteria: {self.policy_criterion} and {self.value_criterion}",
			f"Rollouts:       {self.rollouts}",
			f"Batch size:     {self.batch_size}",
			f"Rollout games:  {self.rollout_games}",
			f"Rollout depth:  {self.rollout_depth}",
			f"Loss weighting: {self.loss_weighting}",
		]))
		self.tt = TickTock()

	def train(self, net: Model) -> (Model, Model):
		"""
		Trains `net` for `rollouts` rollouts each consisting of `rollout_games` games and scrambled for `rollout_depth`.
		Every `evaluation_interval` (or never if evaluation_interval = 0), an evaluation is made of the model at the current stage playing `evaluation_length` games according to `self.evaluator`.
		"""
		self.tt.reset()
		self.tt.tick()
		self.states_per_rollout = (self.rollout_depth) * self.rollout_games
		self.log(f"Beginning training. Optimization is performed in batches of {self.batch_size}")
		self.log("\n".join([
			f"Rollouts: {self.rollouts}",
			f"Each consisting of {self.rollout_games} games with a depth of {self.rollout_depth}",
			f"Evaluations: {len(self.evaluations)}",
		]))
		best_solve = 0
		min_net = net.clone()
		self.agent.update_net(net)
		params = net.get_params()
		orig_params = params

		optimizer = self.optim(net.parameters(), lr=self.lr)
		lr_scheduler = torch.optim.lr_scheduler.StepLR(optimizer, 1, self.gamma)
		self.policy_losses, self.value_losses, self.train_losses, self.eval_rewards, self.avg_value_targets = np.zeros(self.rollouts),\
																											  np.zeros(self.rollouts),\
																											  np.empty(self.rollouts),\
																											  list(), list()
		self.param_changes, self.param_total_changes = list(), list()

		for rollout in range(self.rollouts):
			reset_cuda()

			self.tt.profile("ADI training data")
			training_data, policy_targets, value_targets, loss_weights = self.ADI_traindata(net, rollout)
			self.tt.profile("To cuda")
			training_data, value_targets, policy_targets, loss_weights = training_data.to(gpu),\
																		 value_targets.to(gpu),\
																		 policy_targets.to(gpu),\
																		 loss_weights.to(gpu)
			self.tt.end_profile("To cuda")
			self.tt.end_profile("ADI training data")

			reset_cuda()

			self.tt.profile("Training loop")
			net.train()
			batches = self._get_batches(self.states_per_rollout, self.batch_size)
			for i, batch in enumerate(batches):
				optimizer.zero_grad()
				policy_pred, value_pred = net(training_data[batch], policy = True, value = True)

				# Use loss on both policy and value
				policy_loss = self.policy_criterion(policy_pred, policy_targets[batch]) @ loss_weights[batch]
				value_loss = self.value_criterion(value_pred.squeeze(), value_targets[batch]) @ loss_weights[batch]
				loss = policy_loss + value_loss
				loss.backward()
				optimizer.step()
				self.policy_losses[rollout] += policy_loss.detach().cpu().numpy()
				self.value_losses[rollout] += value_loss.detach().cpu().numpy()
			self.train_losses[rollout] = self.policy_losses[rollout] + self.value_losses[rollout]
			self.tt.end_profile("Training loop")

			# Updates learning rate
			if rollout in np.linspace(0, self.rollouts, self.lr_reductions, dtype=int)[1:-1]:
				lr_scheduler.step()
				lr = optimizer.param_groups[0]["lr"]
				if self.gamma != 1:
					self.log(f"Updated learning rate from {lr/self.gamma:.2e} to {lr:.2e}")

			model_change = torch.sqrt((net.get_params()-params)**2).mean().cpu()
			model_total_change = torch.sqrt((net.get_params()-orig_params)**2).mean().cpu()
			params = net.get_params()
			self.param_changes.append(float(model_change))
			self.param_total_changes.append(model_total_change)

			if self.log.is_verbose() or rollout in (np.linspace(0, 1, 20)*self.rollouts).astype(int):
				self.log(f"Rollout {rollout} completed with weighted loss {self.train_losses[rollout]}")

			if rollout in self.evaluations:
				net.eval()
				self.tt.profile("Target value average")
				targets = value_targets.cpu().numpy()
				self.avg_value_targets.append(np.empty_like(self.depths, dtype=float))
				for i, depth in enumerate(self.depths):
					idcs = np.arange(self.rollout_games) * self.rollout_depth + depth
					self.avg_value_targets[-1][i] = targets[idcs].mean()
				self.tt.end_profile("Target value average")

				self.agent.update_net(net)
				self.tt.profile(f"Evaluating using agent {self.agent}")
				with unverbose:
					eval_results = self.evaluator.eval(self.agent)
				eval_reward = (eval_results != -1).mean()
				self.eval_rewards.append(eval_reward)
				self.tt.end_profile(f"Evaluating using agent {self.agent}")

				if eval_reward > best_solve:
					best_solve = eval_reward
					min_net = net.clone()
					self.log(f"Updated best net with solve rate {eval_reward*100:.2f} % at depth {self.evaluator.scrambling_depths}")


		self.log.verbose("Training time distribution")
		self.log.verbose(self.tt)
		total_time = self.tt.tock()
		eval_time = self.tt.profiles[f'Evaluating using agent {self.agent}'].sum() if len(self.evaluations) else 0
		train_time = self.tt.profiles["Training loop"].sum()
		adi_time = self.tt.profiles["ADI training data"].sum()
		nstates = self.rollouts * self.rollout_games * self.rollout_depth * Cube.action_dim
		states_per_sec = int(nstates / (adi_time+train_time))
		self.log(f"Best net solves {best_solve*100:.2f} % of games at depth {self.evaluator.scrambling_depths}")
		self.log("\n".join([
			f"Total running time:            {self.tt.stringify_time(total_time, 's')}",
			f"- Training data for ADI:       {self.tt.stringify_time(adi_time, 's')} or {adi_time/total_time*100:.2f} %",
			f"- Training time:               {self.tt.stringify_time(train_time, 's')} or {train_time/total_time*100:.2f} %",
			f"- Evaluation time:             {self.tt.stringify_time(eval_time, 's')} or {eval_time/total_time*100:.2f} %",
			f"States witnessed:              {TickTock.thousand_seps(nstates)}",
			f"- States per training second:  {TickTock.thousand_seps(states_per_sec)}",
		]))

		return net, min_net

	def _get_adi_ff_slices(self):
		data_points = self.rollout_games * self.rollout_depth * Cube.action_dim
		slice_size = data_points // self.adi_ff_batches + 1
		# Final slice may have overflow, however this is simply ignored when indexing
		slices = [slice(i*slice_size, (i+1)*slice_size) for i in range(self.adi_ff_batches)]
		return slices

	@no_grad
	def ADI_traindata(self, net, rollout: int):
		"""
		Implements Autodidactic Iteration as per McAleer, Agostinelli, Shmakov and Baldi, "Solving the Rubik's Cube Without Human Knowledge" section 4.1

		Returns games * sequence_length number of observations divided in four arrays:

		torch.tensor: `states` contains the rubiks state for each data point
		np.arrays: `policy_targets` and `value_targets` contains optimal value and policy targets for each training point
		np.array: `loss_weights` contains the weight for each training point (see weighted samples subsection of McAleer et al paper)
		"""

		net.eval()
		self.tt.profile("Scrambling")
		states, oh_states = Cube.sequence_scrambler(self.rollout_games, self.rollout_depth)
		self.tt.end_profile("Scrambling")

		# Keeps track of solved states - Max Lapan's convergence fix
		solved_scrambled_states = (states == Cube.get_solved_instance()).all(axis=tuple(range(1, len(Cube.shape())+1)))
		
		# Generates possible substates for all scrambled states. Shape: n_states*action_dim x *Cube_shape
		self.tt.profile("ADI substates")
		substates = Cube.multi_rotate(np.repeat(states, Cube.action_dim, axis=0), *Cube.iter_actions(len(states)))
		self.tt.end_profile("ADI substates")
		self.tt.profile("One-hot encoding")
		substates_oh = Cube.as_oh(substates)
		self.tt.end_profile("One-hot encoding")

		# Get rewards. 1 for solved states else -1
		self.tt.profile("Reward")
		solved_substates = (substates == Cube.get_solved_instance()).all(axis=tuple(range(1, len(Cube.shape())+1)))
		rewards = torch.ones(*solved_substates.shape)
		rewards[~solved_substates] = -1
		self.tt.end_profile("Reward")
		
		# Generates policy and value targets
		self.tt.profile("ADI feedforward")
		while True:
			try:
				value_parts = [net(substates_oh[slice_], policy=False, value=True).squeeze() for slice_ in self._get_adi_ff_slices()]
				values = torch.cat(value_parts).cpu()
				break
			except RuntimeError:  # Usually caused by running out of vram
				self.log.verbose(f"Increasing number of ADI feed forward batches from {self.adi_ff_batches} to {self.adi_ff_batches*2}")
				self.adi_ff_batches *= 2
		self.tt.end_profile("ADI feedforward")

		self.tt.profile("Calculating targets")
		values += rewards
		values = values.reshape(-1, 12)
		policy_targets = torch.argmax(values, dim=1)
		value_targets = values[np.arange(len(values)), policy_targets]
		value_targets[solved_scrambled_states] = 0
		self.tt.end_profile("Calculating targets")
		
		if self.loss_weighting == "adaptive":
			weighted = np.tile(1 / np.arange(1, self.rollout_depth+1), self.rollout_games)
			unweighted = np.ones_like(weighted)
			alpha = rollout / self.rollouts
			loss_weights = (1-alpha) * weighted + alpha * unweighted
		elif self.loss_weighting == "weighted":
			loss_weights = np.tile(1 / np.arange(1, self.rollout_depth+1), self.rollout_games)
		else:
			loss_weights = np.ones(self.rollout_games*self.rollout_depth)
		loss_weights /= loss_weights.sum()

		return oh_states, policy_targets, value_targets, torch.from_numpy(loss_weights).float()

	def plot_training(self, save_dir: str, title="", semi_logy=False, show=False):
		"""
		Visualizes training by showing training loss + evaluation reward in same plot
		"""
		self.log("Making plot of training")
		ylim = np.array([-0.05, 1.05])
		fig, loss_ax = plt.subplots(figsize=(19.2, 10.8))
		loss_ax.set_xlabel(f"Rollout, each of {TickTock.thousand_seps(self.states_per_rollout)} states")
		loss_ax.set_ylim(ylim*np.max(self.train_losses))

		colour = "red"
		loss_ax.set_ylabel(f"Cross Entropy + MSE loss ({self.loss_weighting})", color = colour)
		loss_ax.plot(self.train_rollouts, self.train_losses, label="Training loss", color=colour)
		loss_ax.plot(self.train_rollouts, self.policy_losses, linestyle="dashdot", label="Policy loss", color="orange")
		loss_ax.plot(self.train_rollouts, self.value_losses, linestyle="dotted", label="Value loss", color="green")
		loss_ax.tick_params(axis='y', labelcolor = colour)
		h1, l1 = loss_ax.get_legend_handles_labels()

		if len(self.evaluations):
			color = 'blue'
			reward_ax = loss_ax.twinx()
			reward_ax.set_ylim(ylim)
			reward_ax.set_ylabel(f"Fraction of {self.evaluator.n_games} won when evaluating at depths {self.evaluator.scrambling_depths} in {self.evaluator.max_time} seconds", color=color)
			reward_ax.plot(self.evaluations, self.eval_rewards, "-o", color=color, label="Fraction of cubes solved")
			reward_ax.tick_params(axis='y', labelcolor=color)
			h2, l2 = reward_ax.get_legend_handles_labels()
			h1 += h2
			l1 += l2
		loss_ax.legend(h1, l1, loc=1)

		fig.tight_layout()
		plt.title(title if title else f"Training - {TickTock.thousand_seps(self.rollouts*self.rollout_games*self.rollout_depth*12)} states")
		if semi_logy: plt.semilogy()
		plt.grid(True)

		os.makedirs(save_dir, exist_ok=True)
		path = os.path.join(save_dir, "training.png")
		plt.savefig(path)
		self.log(f"Saved loss and evaluation plot to {path}")

		if show: plt.show()
		plt.clf()

	def plot_value_targets(self, loc: str, show=False):
		self.log("Plotting average value targets")
		plt.figure(figsize=(19.2, 10.8))
		for target, rollout in zip(self.avg_value_targets, self.evaluations):
			plt.plot(self.depths, target, label=f"Rollout {rollout}")
		plt.legend(loc=1)
		plt.xlabel("Scrambling depth")
		plt.ylabel("Average target value")
		path = os.path.join(loc, "avg_target_values.png")
		plt.savefig(path)
		if show: plt.show()
		plt.clf()
		self.log(f"Saved value target plot to {path}")

	def plot_net_changes(self, loc: str, show=False):
		self.log("Plotting changes to network parameters")
		plt.figure(figsize=(19.2, 10.8))
		plt.plot(self.train_rollouts, np.cumsum(self.param_changes), label="Cumulative change in network parameters")
		plt.plot(self.train_rollouts, self.param_total_changes, linestyle="dashdot", label="Change in parameters since original network")
		plt.legend(loc=2)
		plt.xlabel(f"Rollout, each of {self.states_per_rollout} states")
		plt.ylabel("Euclidian distance")
		plt.grid(True)
		path = os.path.join(loc, "parameter_changes.png")
		plt.savefig(path)
		if show: plt.show()
		plt.clf()
		self.log(f"Saved network change plot to {path}")

	@staticmethod
	def _get_batches(size: int, bsize: int):
		"""
		Generates indices for batch
		"""
		nbatches = int(np.ceil(size/bsize))
		idcs = np.arange(size)
		np.random.shuffle(idcs)
		batches = [slice(batch*bsize, (batch+1)*bsize) for batch in range(nbatches)]
		batches[-1] = slice(batches[-1].start, size)
		return batches



