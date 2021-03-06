# -*- coding: utf-8 -*-

from __future__ import division

import tensorflow as tf
import numpy as np
import logging
import json
import os

class TextAutoencoder(object):
    """
    Class that encapsulates the encoder-decoder architecture to
    reconstruct pieces of text.
    """

    def __init__(self, lstm_units, embeddings, go, num_gpus, train=True):
        """
        Initialize the autoencoder, build tensors here
        :param lstm_units: hidden size of the units
        :param embeddings: size of the vector that represents the words
        :param go: index of the GO symbol in the embedding matrix
        :param num_gpu: number of gpu cards on your machine
        :param train_embeddings: whether to adjust embeddings during training
        :param bidirectional: whether to create a bidirectional autoencoder
            (if False, a simple linear LSTM is used)
        """
        # EOS and GO share the same symbol. Only GO needs to be embedded, and
        # only EOS exists as a possible network output
        self.go = go
        self.eos = go

        self.vocab_size = embeddings.shape[0]
        self.embedding_size = embeddings.shape[1]
        self.num_gpus = num_gpus
        self.lstm_units = lstm_units
        self.g = tf.Graph()
        with self.g.as_default():
            self.build_model(embeddings, train)
            self.saver = tf.train.Saver(self.get_trainable_variables(),
                               max_to_keep=1)

    def build_model(self, embeddings, train=True):
        # the sentence is the object tobe memorized
        self.sentence = tf.placeholder(tf.int32, [self.num_gpus, None, None], 'sentence')
        self.sentence_size = tf.placeholder(tf.int32, [self.num_gpus, None],
                                            'sentence_size')
        self.clip_value = tf.placeholder(tf.float32, name='clip')
        self.learning_rate = tf.placeholder(tf.float32, name='learning_rate')
        self.dropout_keep = tf.placeholder(tf.float32, name='dropout_keep')

        self.decoder_step_input = tf.placeholder(tf.int32,
                                                 [None],
                                                 'prediction_step')
        self.sentence_ = tf.placeholder(tf.int32, [None, None], 'sentence_')
        self.sentence_size_ = tf.placeholder(tf.int32, [None], 'sentence_size_')

        with tf.variable_scope('training_var', reuse=False) as scope:
          with tf.device('/cpu:0'):
            self.global_step = tf.Variable(0, name='gloabal_step', trainable=False)
            name = 'decoder_fw_step_state_c'
            self.decoder_fw_step_c = tf.placeholder(tf.float32,
                                                    [None, self.lstm_units], name)
            name = 'decoder_fw_step_state_h'
            self.decoder_fw_step_h = tf.placeholder(tf.float32,
                                                    [None, self.lstm_units], name)
            self.embeddings = tf.Variable(embeddings, name='embeddings', trainable=True)
            self.opt = tf.train.AdamOptimizer(self.learning_rate)

        if train:
          towerGrads = []
          towerloss = []
          with tf.variable_scope('train') as scope:
            self.lstm_fw = tf.nn.rnn_cell.LSTMCell(self.lstm_units,
                            initializer=tf.glorot_normal_initializer(), reuse=False)
            for i in range(self.num_gpus):
                  with tf.device('/gpu:%d' % i):
                    with tf.name_scope('tower_%d' % i) as scope:
                      self.sentence_run = tf.gather(self.sentence, [i])[0]
                      self.sentence_size_run = tf.gather(self.sentence_size, [i])[0]
                      loss = self.tower(self.sentence_run,
                                   self.sentence_size_run)
                      grads, v = zip(*self.opt.compute_gradients(loss))
                      grads, _ = tf.clip_by_global_norm(grads, self.clip_value)
                      towerGrads.append(zip(grads, v))
                      towerloss.append(loss)
                      # Reuse variables for the next tower
                      tf.get_variable_scope().reuse_variables()
          avgGrad_var_s = self.average_tower_grads(towerGrads)
          self.apply_gradient_op = self.opt.apply_gradients(avgGrad_var_s,
                                              global_step=self.global_step)
          self.loss = tf.reduce_sum(towerloss) / self.num_gpus
          tf.summary.scalar('loss', self.loss)
          self.add_global = self.global_step.assign_add(1)
          self.merged = tf.summary.merge_all()
        else:
          with tf.variable_scope('train') as scope:
              self.lstm_fw = tf.nn.rnn_cell.LSTMCell(self.lstm_units,
                            initializer=tf.glorot_normal_initializer(), reuse=False)
              with tf.device('/cpu:0'):
                  self.sentence_run = self.sentence_
                  self.sentence_size_run = self.sentence_size_
                  _ = self.tower(self.sentence_run, self.sentence_size_run)

    def tower(self, sentence,
              sentence_size):
        with tf.variable_scope('autoencoder') as self.scope:
            embedded = tf.nn.embedding_lookup(self.embeddings, sentence)
            embedded = tf.nn.dropout(embedded, self.dropout_keep)

            # encoding step
            ret = tf.nn.dynamic_rnn(self.lstm_fw, embedded,
                                        dtype=tf.float32,
                                        sequence_length=sentence_size,
                                        scope=self.scope)
            _, self.encoded_state = ret
            encoded_state_fw = self.encoded_state
            fw_scope_name = self.scope

            self.scope.reuse_variables()

            # generate a batch of embedded GO
            # sentence_size has the batch dimension
            go_batch = self._generate_batch_go(sentence_size)
            embedded_eos = tf.nn.embedding_lookup(self.embeddings,
                                                  go_batch)
            embedded_eos = tf.reshape(embedded_eos,
                                      [-1, 1, self.embedding_size])
            decoder_input = tf.concat([embedded_eos, embedded], axis=1)

            # decoding step

            # We give the same inputs to the forward and backward LSTMs,
            # but each one has its own hidden state
            # their outputs are concatenated and fed to the softmax layer
            outputs, _ = tf.nn.dynamic_rnn(
                    self.lstm_fw, decoder_input, sentence_size,
                    encoded_state_fw)

            self.decoder_outputs = outputs

        # now project the outputs to the vocabulary
        with tf.variable_scope('projection') as self.projection_scope:
            # decoder_outputs has shape (batch, max_sentence_size, vocab_size)
            self.logits = tf.layers.dense(outputs, self.vocab_size)

        # tensors for running a model
        embedded_step = tf.nn.embedding_lookup(self.embeddings,
                                               self.decoder_step_input)
        state_fw = tf.nn.rnn_cell.LSTMStateTuple(self.decoder_fw_step_c,
                                                 self.decoder_fw_step_h)

        with tf.variable_scope(fw_scope_name, reuse=True):
            ret_fw = self.lstm_fw(embedded_step, state_fw)
        step_output_fw, self.decoder_fw_step_state = ret_fw

        step_output = step_output_fw

        with tf.variable_scope(self.projection_scope, reuse=True):
            self.projected_step_output = tf.layers.dense(step_output,
                                                         self.vocab_size)

        eos_batch = self._generate_batch_go(sentence_size)
        eos_batch = tf.reshape(eos_batch, [-1, 1])
        decoder_labels = tf.concat([sentence, eos_batch], -1)

        projection_vars = tf.get_collection(tf.GraphKeys.TRAINABLE_VARIABLES,
                                            scope=self.projection_scope.name)
        # a bit ugly, maybe we should improve this?
        projection_w = [var for var in projection_vars
                        if 'kernel' in var.name][0]
        projection_b = [var for var in projection_vars
                        if 'bias' in var.name][0]

        # set the importance of each time step
        # 1 if before sentence end or EOS itself; 0 otherwise
        max_len = tf.shape(sentence)[1]
        mask = tf.sequence_mask(sentence_size + 1, max_len + 1, tf.float32)
        num_actual_labels = tf.reduce_sum(mask)
        projection_w_t = tf.transpose(projection_w)

        # reshape to have batch and time steps in the same dimension
        decoder_outputs2d = tf.reshape(self.decoder_outputs,
                                       [-1, tf.shape(self.decoder_outputs)[-1]])
        labels = tf.reshape(decoder_labels, [-1, 1])
        sampled_loss = tf.nn.sampled_softmax_loss(
            projection_w_t, projection_b, labels, decoder_outputs2d, 100,
            self.vocab_size)

        masked_loss = tf.reshape(mask, [-1]) * sampled_loss
        loss = tf.reduce_sum(masked_loss) / num_actual_labels
        tf.add_to_collection('losses', loss)
        return loss

    def train(self, sess, save_path, train_data, valid_data, batch_size, epochs,
             learning_rate, dropout_keep, clip_value, report_interval):
        """
        Train the model

        :param train_data:Dataset object with training data
        :param valid_data:Dataset object with validation data
        :param batch_size:batch size
        :param epochs: how many epochs to train for
        :param learning_rate: initial learning rate
        :param dropout_keep:the probability that each LSTM input/output is kept
        :param clip_value:value to clip tensor norm during training
        :param report_interval:report after that many batches
        """
        train_writer = tf.summary.FileWriter('summary' + '/train',
                                      sess.graph)
        valid_writer = tf.summary.FileWriter('summary' + '/valid')
        batch_counter = 0
        accumulated_loss = 0
        num_sents = 0
        num_sents_ = 0
        EPOCHS = []
        losses_tra = []
        losses_val = []

        valid_sents, valid_sizes = valid_data.join_all(self.go, shuffle=True)

        train_data.reset_epoch_counter()
        feeds = {self.clip_value:clip_value,
                 self.dropout_keep: dropout_keep,
                 self.learning_rate: learning_rate}

        while train_data.epoch_counter < epochs:
            batch_counter += self.num_gpus

            train_sents = []
            train_sizes = []
            sents, sizes = train_data.next_batch(batch_size, self.num_gpus)
            num_sents_ += len(sents)
            #print(num_sents)
            new_batch_size = len(sents) // self.num_gpus
            if new_batch_size < 10:
                continue
            for i in range(self.num_gpus):
                train_sents.append(sents[i*new_batch_size:(i+1)*new_batch_size])
                train_sizes.append(sizes[i*new_batch_size:(i+1)*new_batch_size])

            feeds[self.sentence] = train_sents
            feeds[self.sentence_size] = train_sizes
            _, loss, summary_tr = sess.run([self.apply_gradient_op, self.loss, self.merged],
                                        feeds)

            # multiply by len because some batches may be smaller
            # (due to bucketing), then take the average
            accumulated_loss += loss * num_sents_
            num_sents += num_sents_
            num_sents_ = 0

            if batch_counter % report_interval == 0:
                avg_loss = accumulated_loss / num_sents
                accumulated_loss = 0
                num_sents = 0

                # we can't use all the validation at once, since it would
                # take too much memory. running many small batches would
                # instead take too much time. So let's just sample it.
                sample_indices = np.reshape(np.random.randint(0, len(valid_data),
                                        50*self.num_gpus), [self.num_gpus, 50])
                valid_sents_ = [valid_sents[sample_indices[i]] for i in
                                range(self.num_gpus)]
                valid_sizes_ = [valid_sizes[sample_indices[i]] for i in
                                range(self.num_gpus)]
                validation_feeds = {
                    self.sentence: valid_sents_,
                    self.sentence_size: valid_sizes_,
                    self.dropout_keep: 1}

                loss, summary_va = sess.run([self.loss, self.merged], validation_feeds)
                train_writer.add_summary(summary_tr, train_data.epoch_counter)
                valid_writer.add_summary(summary_va, train_data.epoch_counter)
                msg = '%d epochs, %d batches\t' % (train_data.epoch_counter,
                                                   batch_counter)
                msg += 'Avg batch loss: %f\t' % avg_loss
                msg += 'Validation loss: %f' % loss

                self.save(sess, save_path)
                msg += '\t(saved model)'
                logging.info(msg)
                EPOCHS.append(train_data.epoch_counter)
                losses_tra.append(avg_loss)
                losses_val.append(loss)
        return EPOCHS, losses_tra, losses_val      

    def save(self, sess, directory):
        """
        Save the autoencoder model and metadata to the specified
        directory
        """
        model_path = os.path.join(directory, 'model')
        self.saver.save(sess, model_path)
        metadata = {'vocab_size': self.vocab_size,
                    'embedding_size': self.embedding_size,
                    'num_units': self.lstm_fw.output_size,
                    'num_gpus': self.num_gpus,
                    'go': self.go,
                    }
        metadata_path = os.path.join(directory, 'metadata.json')
        with open(metadata_path, 'w') as f:
            json.dump(metadata, f)

    @classmethod
    def load(cls, directory):
        """
         Load an instance of this class from a previously saved one.
        :param directory: directory with the model files
        :param session: tensorflow session
        :param train: if True, also create training tensors
        :return: a TextAutoencoder instance
        :return: a session
        """
        model_path = os.path.join(directory, 'model')
        metadata_path = os.path.join(directory, 'metadata.json')
        with open(metadata_path, 'r') as f:
            metadata = json.load(f)
        dummy_embeddings = np.empty((metadata['vocab_size'],
                                     metadata['embedding_size'],),
                                    dtype=np.float32)

        ae = TextAutoencoder(metadata['num_units'], dummy_embeddings,
                             metadata['go'], metadata['num_gpus'], train=False)
        config = tf.ConfigProto()
        config.gpu_options.allow_growth = True
        session = tf.InteractiveSession(graph=ae.g, config=config)
        vars_to_load = ae.get_trainable_variables()

        print(vars_to_load)

        saver = tf.train.Saver(vars_to_load)
        saver.restore(session, model_path)
        return ae, session

    def encode(self, session, inputs, sizes):
        """
        Run the encoder to obtain the encoded hidden state
        :param session: tensorflow session
        :param inputs: 2-d array with the word indices
        :param sizes: 1-d array with size of each sentence
        :return: a 2-d numpy array with the hidden state
        """
        feeds = {self.sentence_: inputs,
                 self.sentence_size_: sizes,
                 self.dropout_keep: 1}
        state = session.run(self.encoded_state, feeds)
        return state.c

    def run(self, session, inputs, sizes):
        """
        Run the autoencoder with the given data
        :param session: tensorflow session
        :param inputs: 2-d array with the word indices
        :param sizes: 1-d array with size of each sentence
        :return: a 2-d array (batch, output_length) with the answer
            produced by the autoencoder. The output length is not
            fixed; it stops after producing EOS for all items in the
            batch or reaching two times the maximum number of time
            steps in the inputs.
        """
        feeds = {self.sentence_: inputs,
                 self.sentence_size_: sizes,
                 self.dropout_keep: 1}
        state = session.run(self.encoded_state, feeds)

        state_fw = state

        time_steps = 0
        max_time_steps = len(inputs[0])
        answer = []
        input_symbol = self.go * np.ones_like(sizes, dtype=np.int32)

        # this array control which sequences have already been finished by the
        # decoder, i.e., for which ones it already produced the END symbol
        sequences_done = np.zeros_like(sizes, dtype=np.bool)
        while True:
            # we could use tensorflow's rnn_decoder, but this gives us
            # finer control

            feeds = {self.decoder_fw_step_c: state_fw.c,
                     self.decoder_fw_step_h: state_fw.h,
                     self.decoder_step_input: input_symbol,
                     self.dropout_keep: 1}
            ops = [self.projected_step_output,
                       self.decoder_fw_step_state]
            outputs, state_fw = session.run(ops, feeds)

            input_symbol = outputs.argmax(1)

            # use an "additive" or in order to avoid infinite loops
            sequences_done |= (input_symbol == self.eos)

            if sequences_done.all() or time_steps >= max_time_steps:
                break
            else:
                time_steps += 1
            answer.append(input_symbol)

        return np.hstack(answer)

    def get_trainable_variables(self):
        """
        Return all trainable variables inside the model
        """
        return tf.get_collection(tf.GraphKeys.TRAINABLE_VARIABLES)

    def _generate_batch_go(self, like):
        """
        Generate a 1-d tensor with copies of EOS as big as the batch size,
        :param like: a tensor whose shape the returned embeddings should match
        :return: a tensor with shape as `like`
        """
        ones = tf.ones_like(like)
        return ones * self.go

    def average_tower_grads(self, tower_grads):
      if(len(tower_grads) == 1):
        return tower_grads[0]
      avgGrad_var_s = []
      for grad_var_s in zip(*tower_grads):
        grads = []
        v = None
        for g, v_ in grad_var_s:
            g = tf.expand_dims(g, 0)
            grads.append(g)
            v = v_
        all_g = tf.concat(grads, 0)
        avg_g = tf.reduce_mean(all_g, 0, keep_dims=False)
        avgGrad_var_s.append((avg_g, v))
      return avgGrad_var_s








  
