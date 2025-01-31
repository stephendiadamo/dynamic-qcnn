import numpy as np
import tensorflow as tf
import tensorflow_quantum as tfq
import cirq
import sympy
from tensorflow import keras


class Qcnn(keras.layers.Layer):
    def __init__(
        self,
        n_q=8,
        s_c=1,
        s_p=0,
        pool_filter="right",
        convolution_mapping=None,
        pooling_mapping=None,
        name="qcnn",
        readout=None,
        ops_gate=cirq.Z,
        theta_init_seed=None,
        theta_range=(0, 2 * np.pi),
        architecture_strategy="binary_tree_r",
        **kwargs,
    ):
        super(Qcnn, self).__init__(name=name, **kwargs)
        self.n_q = n_q
        self.s_c = s_c
        self.s_p = s_p
        self.pool_filter = pool_filter
        if convolution_mapping is None:
            # default convolution layer is defined as U with 10 paramaters. The same U is used in all layers
            # meaning only the first layer needs to be specified
            self.convolution_mapping = {1: (U, 10)}
        else:
            self.convolution_mapping = convolution_mapping
        if pooling_mapping is None:
            self.pooling_mapping = {1: (V, 2)}
        else:
            self.pooling_mapping = pooling_mapping
        # Specify measured wire
        self.readout = readout
        self.ops_gate = ops_gate
        self.theta_init_seed = theta_init_seed
        self.theta_range = theta_range
        self.architecture_strategy = architecture_strategy
        if self.architecture_strategy == "binary_tree_r":
            self.graphs = self._get_binary_tree_r_graphs()
        else:
            raise NotImplementedError(
                f"Architecture strategy {self.architecture_strategy} is not yet implemented"
            )

        self.circuit, self.symbols_ = self._construct_circuit()
        self.theta = self.add_weight(
            shape=(1, len(self.symbols_)),
            initializer=tf.random_uniform_initializer(
                minval=self.theta_range[0],
                maxval=self.theta_range[1],
                seed=self.theta_init_seed,
            ),
            trainable=True,
        )
        self.computation_layer = tfq.layers.ControlledPQC(
            self.circuit, operators=self.ops_gate(self.readout)
        )

    def call(self, inputs):
        """Inputs are encoded as circuits"""
        batch_dim = tf.gather(tf.shape(inputs), 0)
        tiled_up_thetas = tf.tile(self.theta, multiples=[batch_dim, 1])
        expectation_values = self.computation_layer([inputs, tiled_up_thetas])
        return expectation_values

    def _construct_circuit(self):
        circuit = cirq.Circuit()
        total_coef_count = 0
        symbols = ()
        final_layer = max(self.graphs.keys())
        for layer, graph in self.graphs.items():
            # Notational scheme is layer -> C graph --> Qc,Ec, P Graph --> Qp,Ep
            E_cl = graph[0][1]
            E_pl = graph[1][1]
            convolution_block, c_param_count = self.convolution_mapping.get(
                layer, self.convolution_mapping[1]
            )
            pooling_block, p_parm_count = self.pooling_mapping.get(
                layer, self.pooling_mapping[1]
            )
            if c_param_count > 0:
                layer_symbols_c = sympy.symbols(
                    f"x_{total_coef_count}:{total_coef_count + c_param_count}"
                )
                symbols += layer_symbols_c
                total_coef_count = total_coef_count + c_param_count

            if p_parm_count > 0:
                layer_symbols_p = sympy.symbols(
                    f"x_{total_coef_count}:{total_coef_count + p_parm_count}"
                )
                symbols += layer_symbols_p
                total_coef_count = total_coef_count + p_parm_count
            # Convolution Operation
            for bits in E_cl:
                if c_param_count > 0:
                    circuit.append(convolution_block(bits, layer_symbols_c))
                else:
                    # If the circuit has no paramaters then the only argument is bits
                    circuit.append(convolution_block(bits))

            # Pooling Operation
            for bits in E_pl:
                if p_parm_count > 0:
                    circuit.append(pooling_block(bits, layer_symbols_p))
                else:
                    circuit.append(pooling_block(bits))
            if layer == final_layer:
                if self.readout == None:
                    self.readout = cirq.LineQubit(E_pl[0][1])
        return circuit, symbols

    def _get_binary_tree_r_graphs(self):
        """ """
        if type(self.pool_filter) is str:
            # Mapping words to the filter type
            if self.pool_filter == "left":
                # 0 1 2 3 4 5 6 7
                # x x x x
                self.pool_filter = lambda arr: arr[0 : len(arr) // 2 : 1]
            elif self.pool_filter == "right":
                # 0 1 2 3 4 5 6 7
                #         x x x x
                self.pool_filter = lambda arr: arr[len(arr) : len(arr) // 2 - 1 : -1]
            elif self.pool_filter == "even":
                # 0 1 2 3 4 5 6 7
                # x   x   x   x
                self.pool_filter = lambda arr: arr[0::2]
            elif self.pool_filter == "odd":
                # 0 1 2 3 4 5 6 7
                #   x   x   x   x
                self.pool_filter = lambda arr: arr[1::2]
            elif self.pool_filter == "inside":
                # 0 1 2 3 4 5 6 7
                #     x x x x
                self.pool_filter = (
                    lambda arr: arr[
                        len(arr) // 2
                        - len(arr) // 4 : len(arr) // 2
                        + len(arr) // 4 : 1
                    ]
                    if len(arr) > 2
                    else [arr[1]]
                )  # inside
            elif self.pool_filter == "outside":
                # 0 1 2 3 4 5 6 7
                # x x         x x
                self.pool_filter = (
                    lambda arr: [
                        item
                        for item in arr
                        if not (
                            item
                            in arr[
                                len(arr) // 2
                                - len(arr) // 4 : len(arr) // 2
                                + len(arr) // 4 : 1
                            ]
                        )
                    ]
                    if len(arr) > 2
                    else [arr[0]]
                )  # outside

        graphs = {}
        layer = 1
        Qc_l = [i + 1 for i in range(self.n_q)]  # We label the nodes from 1 to n
        Qp_l = Qc_l.copy()
        while len(Qc_l) > 1:

            nq_avaiable = len(Qc_l)
            mod_nq = lambda x: x % nq_avaiable
            Ec_l = [(Qc_l[i], Qc_l[mod_nq(i + self.s_c)]) for i in range(nq_avaiable)]
            if len(Ec_l) == 2 and Ec_l[0][0:] == Ec_l[1][1::-1]:
                Ec_l = [Ec_l[0]]
            measured_q = self.pool_filter(Qc_l)
            remaining_q = [q for q in Qc_l if not (q in measured_q)]
            Ep_l = [
                (measured_q[i], remaining_q[(i + self.s_p) % len(remaining_q)])
                for i in range(len(measured_q))
            ]
            # Convolution graph
            C_l = (Qc_l, Ec_l)
            # Pooling graph
            P_l = (Qp_l, Ep_l)
            # Graph for layer
            G_l = (C_l, P_l)
            graphs[layer] = G_l
            # set avaiable qubits for next layer
            layer = layer + 1
            Qc_l = [j for (i, j) in Ep_l]
            Qp_l = Qc_l.copy()
        return graphs


# Default pooling circuit
def V(bits, symbols=None):
    circuit = cirq.Circuit()
    q0, q1 = cirq.LineQubit(bits[0]), cirq.LineQubit(bits[1])
    circuit += cirq.rz(symbols[0]).on(q1).controlled_by(q0)
    circuit += cirq.X(q0)
    circuit += cirq.rx(symbols[1]).on(q1).controlled_by(q0)
    return circuit

# Default convolution circuit
def U(bits, symbols=None):
    circuit = cirq.Circuit()
    q0, q1 = cirq.LineQubit(bits[0]), cirq.LineQubit(bits[1])
    circuit += cirq.rx(symbols[0]).on(q0)
    circuit += cirq.rx(symbols[1]).on(q1)
    circuit += cirq.rz(symbols[2]).on(q0)
    circuit += cirq.rz(symbols[3]).on(q1)
    circuit += cirq.rz(symbols[4]).on(q1).controlled_by(q0)
    circuit += cirq.rz(symbols[5]).on(q0).controlled_by(q1)
    circuit += cirq.rx(symbols[6]).on(q0)
    circuit += cirq.rx(symbols[7]).on(q1)
    circuit += cirq.rz(symbols[8]).on(q0)
    circuit += cirq.rz(symbols[9]).on(q1)
    return circuit
