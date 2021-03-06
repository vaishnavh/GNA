'''
An example of distribution approximation using Generative Adversarial Networks in TensorFlow.

Based on the blog post by Eric Jang: http://blog.evjang.com/2016/06/generative-adversarial-nets-in.html,
and of course the original GAN paper by Ian Goodfellow et. al.: https://arxiv.org/abs/1406.2661.

The minibatch discrimination technique is taken from Tim Salimans et. al.: https://arxiv.org/abs/1606.03498.
'''
from __future__ import absolute_import
from __future__ import print_function
from __future__ import unicode_literals
from __future__ import division

import argparse
import numpy as np
from scipy.stats import norm
import tensorflow as tf
import matplotlib.pyplot as plt
from matplotlib import animation
import seaborn as sns

sns.set(color_codes=True)

seed = 42
np.random.seed(seed)
tf.set_random_seed(seed)


class DataDistribution(object):
    def __init__(self):
        self.mus = [-6,-3,3,6]
        self.sigma = 0.01

    def sample(self, N):
        sample_mus = np.random.choice(self.mus, N)
        samples = [np.random.normal(mu, self.sigma) for mu in sample_mus]
        samples.sort()
        return samples

    def pdf(self, x):
        return np.mean([norm.pdf(x,loc=self.mus[i], scale=self.sigma) for i in xrange(len(self.mus))])



class GeneratorDistribution(object):
    def __init__(self, range):
        self.range = range

    def sample(self, N):
        return np.linspace(-self.range, self.range, N) + \
            np.random.random(N) * 0.01

with tf.device("/gpu:0"):
    def linear(input, input_dim, output_dim, scope=None, stddev=1):
        #stddev=stddev*(0.000005/float(input_dim*input_dim))
        #norm = tf.random_normal_initializer(stddev=stddev)
        initializer = tf.orthogonal_initializer(0.8)
        const = tf.constant_initializer(0.0)
        with tf.variable_scope(scope or 'linear'):
            w = tf.get_variable('w', [input.get_shape()[1], output_dim], initializer=initializer)
            b = tf.get_variable('b', [output_dim], initializer=const)
            return tf.matmul(input, w) + b


    def generator(input, i_dim, h_dim):
        # 2 layer relu network
        h0 = tf.nn.relu(linear(input, i_dim, h_dim, 'g0'))
        h1 = tf.nn.relu(linear(h0, h_dim, h_dim, 'g1'))
        h2 = linear(h1, h_dim, 1, 'g2')
        return h2


    def discriminator(input, i_dim, h_dim, minibatch_layer=True):
        h0 = tf.nn.relu(linear(input, i_dim, h_dim, 'd0'))
        h1 = tf.nn.relu(linear(h0, h_dim, h_dim, 'd1'))

        # without the minibatch layer, the discriminator needs an additional layer
        # to have enough capacity to separate the two distributions correctly
        if minibatch_layer:
            h2 = minibatch(h1, h_dim)
        else:
            h2 = tf.nn.relu(linear(h1, h_dim, h_dim, scope='d2'))

        h3 = tf.nn.relu(linear(h2, h_dim, 1, scope='d3'))
        return h3


    def minibatch(input, input_dim, num_kernels=5, kernel_dim=3):
        x = linear(input, input_dim, num_kernels * kernel_dim, scope='minibatch', stddev=0.02)
        activation = tf.reshape(x, (-1, num_kernels, kernel_dim))
        diffs = tf.expand_dims(activation, 3) - tf.expand_dims(tf.transpose(activation, [1, 2, 0]), 0)
        abs_diffs = tf.reduce_sum(tf.abs(diffs), 2)
        minibatch_features = tf.reduce_sum(tf.exp(-abs_diffs), 2)
        return tf.concat(1, [input, minibatch_features])




    def optimizer(loss, var_list, initial_learning_rate):
        optimizer = tf.train.AdamOptimizer(initial_learning_rate, beta1=0.5)
        return optimizer



    def optimizer_orig(loss, var_list, initial_learning_rate):
        '''
        decay = 0.95
        num_decay_steps = 150
        batch = tf.Variable(0)
        learning_rate = tf.train.exponential_decay(
            initial_learning_rate,
            batch,
            num_decay_steps,
            decay,
            staircase=True
        )
        '''
        optimizer = tf.train.AdamOptimizer(initial_learning_rate, beta1=0.5).minimize(
            loss,
            #global_step=batch,
            var_list=var_list
        )
        return optimizer



class GAN(object):
    def __init__(self, data, gen, eg, num_steps, batch_size, minibatch, log_every, anim_path):
        self.data = data
        self.gen = gen
        self.eg = eg
        self.num_steps = num_steps
        self.batch_size = batch_size
        self.minibatch = minibatch
        self.log_every = log_every
        self.mlp_hidden_size = 32
        self.anim_path = anim_path
        self.anim_frames = []

        # can use a higher learning rate when not using the minibatch layer
        if self.minibatch:
            self.learning_rate = 0.0001
        else:
            self.learning_rate = 0.005

        self._create_model()

    def _create_model(self):
        # In order to make sure that the discriminator is providing useful gradient
        # information to the generator from the start, we're going to pretrain the
        # discriminator using a maximum likelihood objective. We define the network
        # for this pretraining step scoped as D_pre.
        with tf.variable_scope('D_pre'):
            self.pre_input = tf.placeholder(tf.float32, shape=(self.batch_size, 1))
            self.pre_labels = tf.placeholder(tf.float32, shape=(self.batch_size, 1))
            D_pre = discriminator(self.pre_input, 1, self.mlp_hidden_size, self.minibatch)
            self.pre_loss = tf.reduce_mean(tf.square(D_pre - self.pre_labels))
            self.pre_opt = optimizer_orig(self.pre_loss, None, self.learning_rate)

        # This defines the generator network - it takes samples from a noise
        # distribution as input, and passes them through an MLP.
        with tf.variable_scope('Gen'):
            self.z = tf.placeholder(tf.float32, shape=(self.batch_size, 1))
            self.G = generator(self.z, 1, self.mlp_hidden_size)

        # The discriminator tries two tell the difference between samples from the
        # true data distribution (self.x) and the generated samples (self.z).
        #
        # Here we create two copies of the discriminator network (that share parameters),
        # as you cannot use the same network with different inputs in TensorFlow.
        with tf.variable_scope('Disc') as scope:
            self.x = tf.placeholder(tf.float32, shape=(self.batch_size, 1))
            self.D1 = discriminator(self.x, 1, self.mlp_hidden_size, self.minibatch)
            scope.reuse_variables()
            self.D2 = discriminator(self.G, 1, self.mlp_hidden_size, self.minibatch)

        # Define the loss for discriminator and generator networks (see the original
        # paper for details), and create optimizers for both
        self.loss_d = tf.reduce_mean(-tf.log(self.D1) - tf.log(1 - self.D2))
        self.loss_g = tf.reduce_mean(-tf.log(self.D2))

        self.d_pre_params = tf.get_collection(tf.GraphKeys.TRAINABLE_VARIABLES, scope='D_pre')
        self.d_params = tf.get_collection(tf.GraphKeys.TRAINABLE_VARIABLES, scope='Disc')   # This effectively is a pointer to the variable
        self.g_params = tf.get_collection(tf.GraphKeys.TRAINABLE_VARIABLES, scope='Gen')

        self.opt_d = optimizer(self.loss_d, self.d_params, 1e-4)
        self.opt_g = optimizer(self.loss_g, self.g_params, 1e-4)

        self.opt_d_min = optimizer_orig(self.loss_d, self.d_params, 1e-4)
        self.opt_g_min = optimizer_orig(self.loss_g, self.g_params, 1e-4)

        self.grad_d = self.opt_d.compute_gradients(self.loss_d, self.d_params)
        self.grad_g = self.opt_g.compute_gradients(self.loss_g, self.g_params)

        self.apply_grad_d = self.opt_d.apply_gradients(self.grad_d)
        self.apply_grad_g = self.opt_g.apply_gradients(self.grad_g)

        self.placeholder_d = [tf.placeholder(tf.float32) for i in xrange(len(self.d_params))]
        self.placeholder_g = [tf.placeholder(tf.float32) for i in xrange(len(self.g_params))] #placeholder for array of values

        # {i: tf.placeholder(tf.float32) for i in self.d_params.keys()} #placeholder for array of values

        self.assign_d = [self.d_params[i].assign(self.placeholder_d[i]) for i in xrange(len(self.d_params))]   #[self.d_params[i].assign(self.placeholder_d[i]) for i in self.d_params.keys()] 
        self.assign_g = [self.g_params[i].assign(self.placeholder_g[i]) for i in xrange(len(self.g_params))]   #[self.d_params[i].assign(self.placeholder_d[i]) for i in self.d_params.keys()] 

        #self.saver_d = tf.train.Saver(self.d_params)
        #self.saver_g = tf.train.Saver(self.g_params)

    def compute_gradients(self, session, x, z):
        # run a single update
 
        # Compute the gradients
        session.run([self.grad_d, self.grad_g], {
            self.x: np.reshape(x, (self.batch_size, 1)),
            self.z: np.reshape(z, (self.batch_size, 1))
        })
        



    def apply_gradients(self, session, x, z):
        loss_d, loss_g,_, _ = session.run([self.loss_d,self.loss_g,self.apply_grad_d,self.apply_grad_g], {
                self.x: np.reshape(x, (self.batch_size, 1)),
                self.z: np.reshape(z, (self.batch_size, 1))
            })            
        return loss_d, loss_g


    # TODO
    # Change network
    # Copy hyperparmateters from unrolled GANS paper
    def train(self):
        # This begins a single ssession
        with tf.Session() as session:
            tf.global_variables_initializer().run()

            # pretraining discriminator
            num_pretrain_steps = 0
            for step in xrange(num_pretrain_steps):
                d = (np.random.random(self.batch_size) - 0.5) * 10.0
                labels = [self.data.pdf(dp) for dp in d] #norm.pdf(d, loc=self.data.mu, scale=self.data.sigma)
                pretrain_loss, _ = session.run([self.pre_loss, self.pre_opt], {
                    self.pre_input: np.reshape(d, (self.batch_size, 1)),    
                    self.pre_labels: np.reshape(labels, (self.batch_size, 1))
                })
            self.weightsD = session.run(self.d_pre_params)

            # copy weights from pre-training over to new D network
            for i, v in enumerate(self.d_params):
                session.run(v.assign(self.weightsD[i]))

            if self.eg == True:
                for step in xrange(self.num_steps):

                ## Save discriminator and generator variables
                    x = self.data.sample(self.batch_size)
                    z = self.gen.sample(self.batch_size)
                   
                    #Save original state
                    weightsD = session.run(self.d_params)
                    weightsG = session.run(self.g_params)

                    #Descend a step
                    self.compute_gradients(session,x,z) 
                    self.apply_gradients(session,x,z)

                    x = self.data.sample(self.batch_size)
                    z = self.gen.sample(self.batch_size)
                    #Compute descent at lookahead step
                    self.compute_gradients(session,x,z)

                    # Reassign original state
                    session.run(self.assign_d, dict(zip(self.placeholder_d, weightsD)))
                    session.run(self.assign_g, dict(zip(self.placeholder_g, weightsG)))

                    # Apply lookahead gradient
                    loss_d, loss_g = self.apply_gradients(session,x,z)
                    if step % self.log_every == 0:
                        print('{}: {}\t{}'.format(step, loss_d, loss_g))
                    if self.anim_path:
                        self.anim_frames.append(self._samples(session))
            else:
                for step in xrange(self.num_steps):
                    x = self.data.sample(self.batch_size)
                    z = self.gen.sample(self.batch_size)
                    loss_d, _ = session.run([self.loss_d, self.opt_d_min], {
                        self.x: np.reshape(x, (self.batch_size, 1)),
                        self.z: np.reshape(z, (self.batch_size, 1))
                    })

                    # update generator
                    z = self.gen.sample(self.batch_size)
                    loss_g, _ = session.run([self.loss_g, self.opt_g_min], {
                        self.z: np.reshape(z, (self.batch_size, 1))
                    })

                    

                    if step % self.log_every == 0:
                        print('{}: {}\t{}'.format(step, loss_d, loss_g))

                    if self.anim_path:
                        self.anim_frames.append(self._samples(session))

            if self.anim_path:
                self._save_animation()
            else:
                self._plot_distributions(session)

    def _samples(self, session, num_points=10000, num_bins=100):
        '''
        Return a tuple (db, pd, pg), where db is the current decision
        boundary, pd is a histogram of samples from the data distribution,
        and pg is a histogram of generated samples.
        '''
        xs = np.linspace(-self.gen.range, self.gen.range, num_points)
        bins = np.linspace(-self.gen.range, self.gen.range, num_bins)

        # decision boundary
        db = np.zeros((num_points, 1))
        for i in range(num_points // self.batch_size):
            db[self.batch_size * i:self.batch_size * (i + 1)] = session.run(self.D1, {
                self.x: np.reshape(
                    xs[self.batch_size * i:self.batch_size * (i + 1)],
                    (self.batch_size, 1)
                )
            })

        # data distribution
        d = self.data.sample(num_points)
        pd, _ = np.histogram(d, bins=bins, density=True)

        # generated samples
        zs = np.linspace(-self.gen.range, self.gen.range, num_points)
        g = np.zeros((num_points, 1))
        for i in range(num_points // self.batch_size):
            g[self.batch_size * i:self.batch_size * (i + 1)] = session.run(self.G, {
                self.z: np.reshape(
                    zs[self.batch_size * i:self.batch_size * (i + 1)],
                    (self.batch_size, 1)
                )
            })
        pg, _ = np.histogram(g, bins=bins, density=True)

        return db, pd, pg

    def _plot_distributions(self, session):
        db, pd, pg = self._samples(session)
        db_x = np.linspace(-self.gen.range, self.gen.range, len(db))
        p_x = np.linspace(-self.gen.range, self.gen.range, len(pd))
        f, ax = plt.subplots(1)
        ax.plot(db_x, db, label='decision boundary')
        ax.set_ylim(0, 1)
        plt.plot(p_x, pd, label='real data')
        plt.plot(p_x, pg, label='generated data')
        plt.title('1D Generative Adversarial Network')
        plt.xlabel('Data values')
        plt.ylabel('Probability density')
        plt.legend()
        plt.show()

    def _save_animation(self):
        f, ax = plt.subplots(figsize=(6, 4))
        f.suptitle('1D Generative Adversarial Network -- Extragradient', fontsize=15)
        plt.xlabel('Data values')
        plt.ylabel('Probability density')
        ax.set_xlim(-6, 6)
        ax.set_ylim(0, 1.4)
        line_db, = ax.plot([], [], label='decision boundary')
        line_pd, = ax.plot([], [], label='real data')
        line_pg, = ax.plot([], [], label='generated data')
        frame_number = ax.text(
            0.02,
            0.95,
            '',
            horizontalalignment='left',
            verticalalignment='top',
            transform=ax.transAxes
        )
        ax.legend()

        db, pd, _ = self.anim_frames[0]
        db_x = np.linspace(-self.gen.range, self.gen.range, len(db))
        p_x = np.linspace(-self.gen.range, self.gen.range, len(pd))

        def init():
            line_db.set_data([], [])
            line_pd.set_data([], [])
            line_pg.set_data([], [])
            frame_number.set_text('')
            return (line_db, line_pd, line_pg, frame_number)

        def animate(i):
            frame_number.set_text(
                'Frame: {}/{}'.format(i, len(self.anim_frames))
            )
            db, pd, pg = self.anim_frames[i]
            line_db.set_data(db_x, db)
            line_pd.set_data(p_x, pd)
            line_pg.set_data(p_x, pg)
            return (line_db, line_pd, line_pg, frame_number)

        anim = animation.FuncAnimation(
            f,
            animate,
            init_func=init,
            frames=len(self.anim_frames),
            blit=True
        )
        anim.save(self.anim_path, fps=30, extra_args=['-vcodec', 'libx264'])


def main(args):
    #anim = args.anim
    #if args.eg == True:
    #    anim = anim + '-eg'
    model = GAN(
        DataDistribution(),
        GeneratorDistribution(range=8),
        args.eg,
        args.num_steps,
        args.batch_size,
        args.minibatch,
        args.log_every,
        args.anim
    )
    model.train()


def parse_args():
    parser = argparse.ArgumentParser()
    parser.add_argument('--eg', type=bool, default=False,
                        help='Extragradient descent')
    parser.add_argument('--num-steps', type=int, default=3000,
                        help='the number of training steps to take')
    parser.add_argument('--batch-size', type=int, default=12,
                        help='the batch size')
    parser.add_argument('--minibatch', type=bool, default=False,
                        help='use minibatch discrimination')
    parser.add_argument('--log-every', type=int, default=100,
                        help='print loss after this many steps')
    parser.add_argument('--anim', type=str, default=None,
                        help='name of the output animation file (default: none)')
    return parser.parse_args()


if __name__ == '__main__':
    main(parse_args())
