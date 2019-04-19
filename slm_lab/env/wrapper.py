# Module of custom Atari wrappers modified from OpenAI baselines (MIT)
# these don't come with Gym but are crucial for Atari to work
#  https://github.com/openai/baselines/blob/master/baselines/common/atari_wrappers.py
from collections import deque
from gym import spaces
from slm_lab.lib import util
import gym
import numpy as np


class NoopResetEnv(gym.Wrapper):
    def __init__(self, env, noop_max=30):
        '''Sample initial states by taking random number of no-ops on reset.
        No-op is assumed to be action 0.
        '''
        gym.Wrapper.__init__(self, env)
        self.noop_max = noop_max
        self.override_num_noops = None
        self.noop_action = 0
        assert env.unwrapped.get_action_meanings()[0] == 'NOOP'

    def reset(self, **kwargs):
        '''Do no-op action for a number of steps in [1, noop_max].'''
        self.env.reset(**kwargs)
        if self.override_num_noops is not None:
            noops = self.override_num_noops
        else:
            noops = self.unwrapped.np_random.randint(1, self.noop_max + 1)  # pylint: disable=E1101
        assert noops > 0
        obs = None
        for _ in range(noops):
            obs, _, done, _ = self.env.step(self.noop_action)
            if done:
                obs = self.env.reset(**kwargs)
        return obs

    def step(self, ac):
        return self.env.step(ac)


class FireResetEnv(gym.Wrapper):
    def __init__(self, env):
        '''Take action on reset for environments that are fixed until firing.'''
        gym.Wrapper.__init__(self, env)
        assert env.unwrapped.get_action_meanings()[1] == 'FIRE'
        assert len(env.unwrapped.get_action_meanings()) >= 3

    def reset(self, **kwargs):
        self.env.reset(**kwargs)
        obs, _, done, _ = self.env.step(1)
        if done:
            self.env.reset(**kwargs)
        obs, _, done, _ = self.env.step(2)
        if done:
            self.env.reset(**kwargs)
        return obs

    def step(self, ac):
        return self.env.step(ac)


class EpisodicLifeEnv(gym.Wrapper):
    def __init__(self, env):
        '''Make end-of-life == end-of-episode, but only reset on true game over.
        Done by DeepMind for the DQN and co. since it helps value estimation.
        '''
        gym.Wrapper.__init__(self, env)
        self.lives = 0
        self.was_real_done = True

    def step(self, action):
        obs, reward, done, info = self.env.step(action)
        self.was_real_done = done
        # check current lives, make loss of life terminal,
        # then update lives to handle bonus lives
        lives = self.env.unwrapped.ale.lives()
        if lives < self.lives and lives > 0:
            # for Qbert sometimes we stay in lives == 0 condtion for a few frames
            # so its important to keep lives > 0, so that we only reset once
            # the environment advertises done.
            done = True
        self.lives = lives
        return obs, reward, done, info

    def reset(self, **kwargs):
        '''Reset only when lives are exhausted.
        This way all states are still reachable even though lives are episodic,
        and the learner need not know about any of this behind-the-scenes.
        '''
        if self.was_real_done:
            obs = self.env.reset(**kwargs)
        else:
            # no-op step to advance from terminal/lost life state
            obs, _, _, _ = self.env.step(0)
        self.lives = self.env.unwrapped.ale.lives()
        return obs


class MaxAndSkipEnv(gym.Wrapper):
    '''
    OpenAI max-skipframe wrapper from baselines (not available from gym itself)
    '''

    def __init__(self, env, skip=4):
        '''Return only every `skip`-th frame'''
        gym.Wrapper.__init__(self, env)
        # most recent raw observations (for max pooling across time steps)
        self._obs_buffer = np.zeros((2,) + env.observation_space.shape, dtype=np.uint8)
        self._skip = skip

    def step(self, action):
        '''Repeat action, sum reward, and max over last observations.'''
        total_reward = 0.0
        done = None
        for i in range(self._skip):
            obs, reward, done, info = self.env.step(action)
            if i == self._skip - 2:
                self._obs_buffer[0] = obs
            if i == self._skip - 1:
                self._obs_buffer[1] = obs
            total_reward += reward
            if done:
                break
        # Note that the observation on the done=True frame doesn't matter
        max_frame = self._obs_buffer.max(axis=0)
        return max_frame, total_reward, done, info

    def reset(self, **kwargs):
        return self.env.reset(**kwargs)


class ClipRewardEnv(gym.RewardWrapper):
    def reward(self, reward):
        '''Atari reward, to -1, 0 or +1. Not usually used as SLM Lab memory class does the clipping'''
        return np.sign(reward)


class TransformImage(gym.ObservationWrapper):
    def __init__(self, env):
        '''
        Apply image preprocessing:
        - grayscale
        - downsize to 84x84
        - transform shape from w,h,c to PyTorch format c,h,w '''
        gym.ObservationWrapper.__init__(self, env)
        self.width = 84
        self.height = 84
        self.observation_space = spaces.Box(
            low=0, high=255, shape=(1, self.width, self.height), dtype=np.uint8)

    def observation(self, frame):
        frame = util.transform_image(frame, method='openai')
        frame = np.transpose(frame)  # reverses all axes
        frame = np.expand_dims(frame, 0)
        return frame


class LazyFrames(object):
    def __init__(self, frames):
        '''This object ensures that common frames between the observations are only stored once.
        It exists purely to optimize memory usage which can be huge for DQN's 1M frames replay buffers.
        This object should only be converted to numpy array before being passed to the model.
        '''
        self._frames = frames
        self._out = None

    def _force(self):
        if self._out is None:
            self._out = np.concatenate(self._frames, axis=0)
            self._frames = None
        return self._out

    def __array__(self, dtype=None):
        out = self._force()
        if dtype is not None:
            out = out.astype(dtype)
        return out

    def __len__(self):
        return len(self._force())

    def __getitem__(self, i):
        return self._force()[i]


class FrameStack(gym.Wrapper):
    def __init__(self, env, k):
        '''Stack k last frames. Returns lazy array, which is much more memory efficient.'''
        gym.Wrapper.__init__(self, env)
        self.k = k
        self.frames = deque([], maxlen=k)
        shp = env.observation_space.shape
        self.observation_space = spaces.Box(
            low=0, high=255, shape=(k, ) + shp[1:], dtype=env.observation_space.dtype)

    def reset(self):
        ob = self.env.reset()
        for _ in range(self.k):
            self.frames.append(ob)
        return self._get_ob()

    def step(self, action):
        ob, reward, done, info = self.env.step(action)
        self.frames.append(ob)
        return self._get_ob(), reward, done, info

    def _get_ob(self):
        assert len(self.frames) == self.k
        return LazyFrames(list(self.frames))


def wrap_atari(env):
    '''Apply a common set of wrappers for Atari games'''
    assert 'NoFrameskip' in env.spec.id
    env = NoopResetEnv(env, noop_max=30)
    env = MaxAndSkipEnv(env, skip=4)
    return env


def wrap_deepmind(env, episode_life=True, clip_rewards=True, stack_len=None):
    '''Wrap Atari environment DeepMind-style'''
    if episode_life:
        env = EpisodicLifeEnv(env)
    if 'FIRE' in env.unwrapped.get_action_meanings():
        env = FireResetEnv(env)
    if clip_rewards:
        env = ClipRewardEnv(env)
    env = TransformImage(env)
    if stack_len is not None:
        env = FrameStack(env, stack_len)
    return env


def make_env(name, seed, stack_len=None):
    env = gym.make(name)
    env.seed(seed)
    if 'NoFrameskip' in env.spec.id:  # for Atari
        env = wrap_atari(env)
        # no reward clipping in training since Atari Memory classes handle it
        if util.get_lab_mode() == 'eval':
            env = wrap_deepmind(env, stack_len=stack_len, clip_rewards=False, episode_life=False)
        else:
            env = wrap_deepmind(env, stack_len=stack_len, clip_rewards=False, episode_life=True)
    return env
