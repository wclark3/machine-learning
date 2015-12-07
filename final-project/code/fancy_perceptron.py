#!/usr/bin/env python
import json

import lasagne
import theano
import theano.tensor as T

import adaptive_updates
import perceptron


class FancyPerceptron(perceptron.MultiLevelPerceptron):
    __LINEARITY_TYPES = {
        'rectify': lasagne.nonlinearities.rectify,
        'tanh': lasagne.nonlinearities.tanh,
        'leaky_rectify': lasagne.nonlinearities.leaky_rectify,
        'sigmoid': lasagne.nonlinearities.sigmoid
    }

    # This isn't great, but it's a one-off
    def __init__(self, config, input_shape, output_vars):
        super(FancyPerceptron, self).__init__()
        self.__config = config
        self.__input_shape = (config['batchsize'],) + input_shape
        self.__output_vars = output_vars

        self.__input_var = T.tensor4('input')
        self.__target_var = T.matrix('target')

        self._output_coords = None
        self._output_exists = None
        self._network = None
        self._create_network()
        self.__train_fn = None
        self.__validate_fn = None
        self.__adaptive_updates = []

    def _create_network(self):
        if self._network is not None:
            raise AssertionError('Cannot call BuildNetwork more than once')

        # pylint: disable=redefined-variable-type
        nonlinearity = self.__LINEARITY_TYPES[self.__config['nonlinearity']]

        # Input Layer
        lyr = lasagne.layers.InputLayer(self.__input_shape, self.__input_var,
                                        name='input')
        if 'input_drop_rate' in self.__config:
            lyr = lasagne.layers.DropoutLayer(
                lyr,
                p=self.__config['input_drop_rate'],
                name='input_dropout')

        # 2d Convolutional Layers
        if 'conv' in self.__config:
            i = 0
            for conv in self.__config['conv']:
                lyr = lasagne.layers.Conv2DLayer(
                    lyr,
                    num_filters=conv['filter_count'],
                    filter_size=tuple(conv['filter_size']),
                    nonlinearity=nonlinearity,
                    name=('conv_2d_%d' % i))
                if 'pooling_size' in conv:
                    lyr = lasagne.layers.MaxPool2DLayer(
                        lyr,
                        pool_size=tuple(conv['pooling_size']),
                        name=('pool_2d_%d' % i))
                if 'dropout' in conv and conv['dropout'] != 0:
                    lyr = lasagne.layers.DropoutLayer(
                        lyr,
                        p=conv['dropout'],
                        name=('conv_dropout_%d' % i))
                i += 1

        # Hidden Layers
        if 'hidden' in self.__config:
            i = 0
            for hidden in self.__config['hidden']:
                lyr = lasagne.layers.DenseLayer(
                    lyr,
                    num_units=hidden['width'],
                    nonlinearity=nonlinearity,
                    W=lasagne.init.GlorotUniform(),
                    name=('dense_%d' % i))

                if 'dropout' in hidden and hidden['dropout'] != 0:
                    lyr = lasagne.layers.DropoutLayer(
                        lyr,
                        p=hidden['dropout'],
                        name=('dropout_%d' % i))
                i += 1

        # Output Layer for Exists
        self._output_exists = lasagne.layers.DenseLayer(
            lyr, num_units=self.__output_vars/2,
            nonlinearity=lasagne.nonlinearities.sigmoid,
            name="output_exists")

        # Output Layer For Coordinates
        output_coords_norm = lasagne.layers.DenseLayer(
            lyr, num_units=self.__output_vars,
            nonlinearity=None,
            name='output_coords_norm')

        class OutputScaledOffsetLayer(lasagne.layers.Layer):
            def get_output_for(self, input, **kwargs):
                return (input + 1.) * 48.

        self._output_coords = OutputScaledOffsetLayer(
            output_coords_norm, name="output_coords")

        # Combined Output Layer
        self._network = lasagne.layers.ConcatLayer(
            [self._output_coords, self._output_exists],
            name='output')

    def epoch_done_tasks(self, epoch, num_epochs):
        for updater in self.__adaptive_updates:
            updater.update(epoch, num_epochs)

    @staticmethod
    def fancy_objective(predictions, targets):
        loss = lasagne.objectives.binary_crossentropy(
            predictions[:, 30:45], targets[:, 30:45]).mean() * 100.
        loss += lasagne.objectives.squared_error(
            predictions[:, 0:30], targets[:, 0:30]).mean()
        return loss

    def build_network(self):
        # The output of the entire network is the prediction, define loss to be
        # the RMSE of the predicted values + optional l1/l2 penalties.
        prediction = lasagne.layers.get_output(self._network)
        loss = FancyPerceptron.fancy_objective(prediction, self.__target_var)

        if 'l1' in self.__config and self.__config['l1']:
            print "Enabling L1 Regularization"
            loss += lasagne.regularization.regularize_network_params(
                self._network, lasagne.regularization.l1)

        if 'l2' in self.__config and self.__config['l2']:
            print "Enabling L2 Regularization"
            loss += lasagne.regularization.regularize_network_params(
                self._network, lasagne.regularization.l2) * 1e-4

        # Grab the parameters and define the update scheme.
        params = lasagne.layers.get_all_params(self._network, trainable=True)
        shared_learning_rate, learning_rate_updater = (
            adaptive_updates.adaptive_update_factory(
                'learning_rate', self.__config))
        if learning_rate_updater is not None:
            self.__adaptive_updates.append(learning_rate_updater)

        shared_momentum, momentum_updater = (
            adaptive_updates.adaptive_update_factory(
                'momentum', self.__config))
        if momentum_updater is not None:
            self.__adaptive_updates.append(momentum_updater)

        updates = lasagne.updates.nesterov_momentum(
            loss, params, learning_rate=shared_learning_rate,
            momentum=shared_momentum)

        # For testing the output, use the deterministic parts of the output
        # (this turns off noise-sources, if we had any and possibly does things
        # related to dropout layers, etc.).  Again, loss is defined using rmse.
        output_det = lasagne.layers.get_output(
            self._network, deterministic=True)
        coord_rmse = lasagne.objectives.squared_error(
            output_det, self.__target_var)[:, 0:30]
        exists_loss = lasagne.objectives.binary_crossentropy(
            output_det, self.__target_var)[:, 30:45]
        combo_loss = FancyPerceptron.fancy_objective(
            output_det, self.__target_var)

        # Create the training and validation functions that we'll use to train
        # the model and validate the results.
        self.__train_fn = theano.function(
            [self.__input_var, self.__target_var],
            [loss, coord_rmse, exists_loss], updates=updates)
        self.__validate_fn = theano.function(
            [self.__input_var, self.__target_var],
            [combo_loss, coord_rmse, exists_loss])

    def predict(self, x_values):
        return(lasagne.layers.get_output(
            self._network, x_values, deterministic=True).eval())

    def train(self, x_values, y_values):
        return self.__train_fn(x_values, y_values)

    def validate(self, x_values, y_values):
        return self.__validate_fn(x_values, y_values)

    def get_state(self):
        return lasagne.layers.get_all_param_values(self._network)

    def set_state(self, state):
        lasagne.layers.set_all_param_values(self._network, state)

    def __str__(self):
        ret_string = "Convoluational MLP:\n%s\n" % (
            json.dumps(self.__config, sort_keys=True))

        lyrs = lasagne.layers.get_all_layers(self._network)
        ret_string += "  Layer Shapes:\n"
        for lyr in lyrs:
            ret_string += "\t%20s = %s\n" % (
                lyr.name, lasagne.layers.get_output_shape(lyr))
        return ret_string