import numpy as np

from librubiks.cube import Cube
from librubiks.solving.search import Searcher, DeepSearcher


class Agent:

	def __init__(self, searcher: Searcher):
		"""
		time_limit: Number of seconds that the tree search part of the algorithm is allowed to search
		"""
		self._searcher = searcher

	def generate_action_queue(self, state: np.ndarray, time_limit: float, max_states: int=None) -> (bool, int):
		solution_found = self._searcher.search(state, time_limit, max_states)
		return solution_found, len(self._searcher.action_queue)

	def action(self) -> (int, bool):
		return Cube.action_space[self._searcher.action_queue.popleft()]

	def actions(self) -> (int, bool):
		while self._searcher.action_queue:
			yield self.action()

	def __str__(self):
		return str(self._searcher)

	def __len__(self):
		# Returns number of explored states
		return len(self._searcher)


class DeepAgent(Agent):
	def __init__(self, searcher: DeepSearcher):
		super().__init__(searcher)

	def update_net(self, net):
		self._searcher.net = net


