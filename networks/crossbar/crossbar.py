"""
crossbar.py
Louis Primeau
University of Toronto Department of Electrical and Computer Engineering
louis.primeau@mail.utoronto.ca
July 29th 2020
"""

import torch
import numpy as np
import itertools
import time
# Implements scipy's minmax scaler except just between 0 and 1 for torch Tensors.
# Taken from a ptrblck post on the PyTorch forums. Love that dude.
class MinMaxScaler(object):
    def __call__(self, tensor):
        self.scale = 1.0 / (tensor.max(dim=1, keepdim=True)[0] - tensor.min(dim=1, keepdim=True)[0])
        self.min = tensor.min(dim=1, keepdim=True)[0]
        tensor.sub_(self.min).mul_(self.scale)
        return tensor
    def inverse_transform(self, tensor):
        tensor.div_(self.scale).add_(self.min)
        return tensor

class crossbar:
    def __init__(self, device_params):
        
        # Power Supply Voltage
        self.V = device_params["Vdd"]

        # DAC resolution
        self.input_resolution = device_params["dac_resolution"]
        self.output_resolution = device_params["adc_resolution"]

        # Wordline Resistance 
        self.r_wl = torch.Tensor((device_params["r_wl"],))
        # Bitline Resistance
        self.r_bl = torch.Tensor((device_params["r_bl"],))

        # Number of rows, columns
        self.size = device_params["m"], device_params["n"]

        # High resistance state
        self.g_on = 1 / torch.normal(device_params["r_on_mean"], device_params["r_on_stddev"], size=self.size)
        #self.g_on = (1 / device_params["r_on_mean"]) * torch.ones(self.size)
        
        # Low Resistance state
        self.g_off = 1 / torch.normal(device_params["r_off_mean"], device_params["r_off_stddev"], size=self.size)
        #self.g_off = (1 / device_params["r_off_mean"]) * torch.ones(self.size)
        
        self.g_wl = torch.Tensor((1 / device_params["r_wl"],))
        self.g_bl = torch.Tensor((1 / device_params["r_bl"],))
        
        # Resolution
        self.resolution = device_params["device_resolution"]
        # Conductance tensor, m x n x 2**resolution        

        # 2**self.resolution - 1 so that there's a conductance state in the middle.
        self.conductance_states = torch.cat([torch.cat([torch.linspace(self.g_off[i,j], self.g_on[i,j],2**self.resolution - 1).unsqueeze(0)
                                                        for j in range(self.size[1])],dim=0).unsqueeze(0)
                                             for i in range(self.size[0])],dim=0)

        # Bias Scheme
        self.bias_voltage = self.V * device_params["bias_scheme"]
        
        # Tile size (1x1 = 1T1R, nxm = passive, etc.)
        self.tile_rows = device_params["tile_rows"]
        self.tile_cols = device_params["tile_cols"]
        assert self.size[0] % self.tile_rows == 0, "tile size does not divide crossbar size in row direction"
        assert self.size[1] % self.tile_cols == 0, "tile size does not divide crossbar size in col direction"
        
        # Resistance of CMOS lines
        self.r_cmos_line = device_params["r_cmos_line"]

        # Conductance Matrix; initialize each memristor at the on resstance
        self.W = torch.ones(self.size) * self.g_on

        # Stuck-on & stuck-on device nonideality 
        self.p_stuck_on = device_params["p_stuck_on"]
        self.p_stuck_off = device_params["p_stuck_off"]
        self.devicefaults = False

        self.mapped = []
        self.tensors = [] #original data of all mapped weights
        self.saved_tiles = {}
        
    def apply_stuck(self, p_stuck_on, p_stuck_off):

        state_dist = torch.distributions.categorical.Categorical(probs=torch.Tensor([p_stuck_on, p_stuck_off, 1 - p_stuck_on - p_stuck_off]))
        state_mask = state_dist.sample(self.size)

        self.W[state_mask == 0] = self.g_off[state_mask==0]
        self.W[state_mask == 1] = self.g_on[state_mask==1]
        
        return None
    
    def map(self, matrix):
        assert not(matrix.size(0) > self.size[0] or matrix.size(1)*2 > self.size[1]), "input too large"
        midpoint = self.conductance_states.size(2) // 2
        
        for i in range(matrix.size(0)):
            for j in range(matrix.size(1)):

                shifted = self.conductance_states[i,j] - self.conductance_states[i,j,midpoint]
                idx = torch.min(torch.abs(shifted - matrix[i,j]), dim=0)[1]    

                self.W[i,2*j+1] = self.conductance_states[i,j,idx]
                self.W[i,2*j] = self.conductance_states[i,j,midpoint-(idx-midpoint)]

    def solve(self, voltage):
        output = torch.zeros((voltage.size(1), self.size[1]))
        for i in range(self.size[0] // self.tile_rows):
            for j in range(self.size[1] // self.tile_cols):
                for k in range(voltage.size(1)):

                    coords = (i*self.tile_rows, (i+1)*self.tile_rows, j*self.tile_cols, (j+1)*self.tile_rows)
                    vect = voltage[i*self.tile_rows:(i+1)*self.tile_rows,k]
                    solution = self.circuit_solve(coords, vect, torch.zeros(self.size[1]), torch.ones(self.size[1]), torch.zeros(self.size[0]))
                    output[k] += torch.cat((torch.zeros(j*self.tile_cols), solution, torch.zeros((self.size[1] // self.tile_cols - j - 1) * self.tile_cols)))

        return output

    """
    A Comprehensive Crossbar Array Model With Solutions for Line Resistance and Nonlinear Device Characteristics
    An Chen
    IEEE TRANSACTIONS ON ELECTRON DEVICES, VOL. 60, NO. 4, APRIL 2013
    """
    
    def hash_M(self, a, b, c, d):
        return str(a) + "_" + str(b) + "_" + str(c) + "_" + str(d)
    
    def make_M(self, a, b, c, d):
        
        conductances = self.W[a:b,c:d]
        g_wl, g_bl = self.g_wl, self.g_bl
        g_s_wl_in, g_s_wl_out = torch.ones(self.tile_rows) * 1, torch.ones(self.tile_rows) * 1e-9
        g_s_bl_in, g_s_bl_out = torch.ones(self.tile_rows) * 1e-9, torch.ones(self.tile_rows) * 1
        m, n = self.tile_rows, self.tile_cols
        
        A = torch.block_diag(*tuple(torch.diag(conductances[i,:])
                          + torch.diag(torch.cat((g_wl, g_wl * 2 * torch.ones(n-2), g_wl)))
                          + torch.diag(g_wl * -1 *torch.ones(n-1), diagonal = 1)
                          + torch.diag(g_wl * -1 *torch.ones(n-1), diagonal = -1)
                          + torch.diag(torch.cat((g_s_wl_in[i].view(1), torch.zeros(n - 2), g_s_wl_out[i].view(1))))
                                   for i in range(m)))

        B = torch.block_diag(*tuple(-torch.diag(conductances[i,:]) for i in range(m)))
        
        def makec(j):
            c = torch.zeros(m, m*n)
            for i in range(m):
                c[i,n*(i) + j] = conductances[i,j]
            return c
  
        C = torch.cat([makec(j) for j in range(n)],dim=0)
        
        def maked(j):
            d = torch.zeros(m, m*n)

            def c(k): 
                return(k - 1)
            
            i = 1
            d[c(i),c(j)] = -g_s_bl_in[c(j)] - g_bl - conductances[c(i),c(j)]
            d[c(i), n*i + c(j)] = g_bl

            i = m
            d[c(i), n*(i-2) + c(j)] = g_bl
            d[c(i), n*(i-1) + c(j)] = -g_s_bl_out[c(j)] - conductances[c(i),c(j)] - g_bl

            for i in range(2, m):
                d[c(i), n*(i-2) + c(j)] = g_bl
                d[c(i), n*(i-1) + c(j)] = -g_bl - conductances[c(i),c(j)] - g_bl
                d[c(i), n*(i) + c(j)] = g_bl

            return d

        D = torch.cat([maked(j) for j in range(1,n+1)], dim=0)

        M = torch.cat((torch.cat((A,B),dim=1), torch.cat((C,D),dim=1)), dim=0)
        
        self.saved_tiles[self.hash_M(a,b,c,d)] = M
        
        return torch.inverse(M)
    
    def circuit_solve(self, coords,  v_wl_in, v_bl_in, v_bl_out, v_wl_out):
        
        g_wl, g_bl = self.g_wl, self.g_bl
        g_s_wl_in, g_s_wl_out = torch.ones(self.tile_rows) * 1, torch.ones(self.tile_rows) * 1e-9
        g_s_bl_in, g_s_bl_out = torch.ones(self.tile_rows) * 1e-9, torch.ones(self.tile_rows) * 1
        m, n = self.tile_rows, self.tile_cols
        
        
        if self.hash_M(*coords) not in self.saved_tiles.keys():
            M = self.make_M(*coords)
        else:
            M = self.saved_tiles[self.hash_M(*coords)]
        
        E = torch.cat([torch.cat(((v_wl_in[i]*g_s_wl_in[i]).view(1), #EW
                                  torch.zeros(n-2),
                                  (v_wl_out[i]*g_s_wl_out[i]).view(1)))
                                 for i in range(m)] +
                      [torch.cat(((-v_bl_in[i]*g_s_bl_in[i]).view(1), #EB
                                  torch.zeros(m-2),
                                  (-v_bl_in[i]*g_s_bl_out[i]).view(1)))
                                 for i in range(n)]
        ).view(-1, 1)
        
        V = torch.matmul(M, E)
        
        V = torch.chunk(torch.solve(E, M)[0], 2)

        return torch.sum((V[1] - V[0]).view(m,n)*self.W[coords[0]:coords[1],coords[2]:coords[3]],dim=0)

    def register_linear(self, matrix, bias=None):

        self.tensors.append(matrix)
        row, col = self.find_space(matrix.size(0), matrix.size(1))
        # Need to add checks for bias size and col size
        
        # Scale matrix                                    
        mat_scale_factor = torch.max(torch.abs(matrix)) / torch.max(self.g_on) * 2
        scaled_matrix = matrix / mat_scale_factor
        
        midpoint = self.conductance_states.size(2) // 2
        for i in range(row, row + scaled_matrix.size(0)):
            for j in range(col, col + scaled_matrix.size(1)):
                
                shifted = self.conductance_states[i,j] - self.conductance_states[i,j,midpoint]
                idx = torch.min(torch.abs(shifted - scaled_matrix[i-row,j-col]), dim=0)[1]
                self.W[i,2*j+1] = self.conductance_states[i,j,idx]
                self.W[i,2*j] = self.conductance_states[i,j,midpoint-(idx-midpoint)]

        
        return ticket(row, col, matrix.size(0), matrix.size(1), matrix, mat_scale_factor, self)

    def which_tiles(self, row, col, m_row, m_col):
        return itertools.product(range(row // self.tile_rows, (row + m_row) // self.tile_rows + 1),
                                 range(col // self.tile_cols,(col + m_col) // self.tile_cols + 1),
        )

    def find_space(self, m_row, m_col):
        if not self.mapped:
            self.mapped.append((0,0,m_row,m_col))
        else:
            self.mapped.append((self.mapped[-1][0] + self.mapped[-1][2], self.mapped[-1][1] + self.mapped[-1][3], m_row, m_col))
        return self.mapped[-1][0], self.mapped[-1][1] 
    
    def clear(self):
        self.mapped = []
        self.tensors = []
        self.W = torch.ones(self.size) * self.g_on
         
       
class ticket:
    def __init__(self, row, col, m_rows, m_cols, matrix, mat_scale_factor, crossbar):
        self.row, self.col = row, col
        self.m_rows, self.m_cols = m_rows, m_cols
        self.crossbar = crossbar
        self.mat_scale_factor = mat_scale_factor
        self.matrix = matrix
    def prep_vector(self, vector, v_bits):

        # Scale vector to [0, 2^v_bits]
        vect_min = torch.min(vector)
        vector = vector - vect_min        
        vect_scale_factor = torch.max(vector) / (2**v_bits - 1)
        vector = vector / vect_scale_factor if vect_scale_factor != 0.0 else vector

        # decompose vector by bit
        bit_vector = torch.zeros(vector.size(0),v_bits)
        bin2s = lambda x : ''.join(reversed( [str((int(x) >> i) & 1) for i in range(v_bits)] ) )
        for j in range(vector.size(0)):
            bit_vector[j,:] = torch.Tensor([float(i) for i in list(bin2s(vector[j]))])
        bit_vector *= self.crossbar.V
        
        # Pad bit vector with unselected voltages
        pad_vector = torch.zeros(self.crossbar.size[0], v_bits)
        pad_vector[self.row:self.row + self.m_rows,:] = bit_vector

        return pad_vector, vect_scale_factor, vect_min
    
    def vmm(self, vector, v_bits=4):
        assert vector.size(1) == 1, "vector wrong shape";
        
        crossbar = self.crossbar
        
        # Rescale vector and convert to bits.
        pad_vector, vect_scale_factor, vect_min = self.prep_vector(vector, v_bits)

        # Solve crossbar circuit
        output = crossbar.solve(pad_vector)

        # Get relevant output columns and add binary outputs
        output = output.view(v_bits, -1, 2)[:,:,0] - output.view(v_bits, -1,2)[:,:,1]
        for i in range(output.size(0)):
            output[i] *= 2**(v_bits - i - 1)
        output = torch.sum(output, axis=0)[self.col:self.col + self.m_cols] 
        
        # Rescale output
        output = (output / crossbar.V * vect_scale_factor * self.mat_scale_factor) / 1.6411 + torch.sum(vect_min * self.matrix, axis=0)

        return output.view(-1, 1)
    
device_params = {"Vdd": 1.8,
                 "r_wl": 20,
                 "r_bl": 20,
                 "m": 16,
                 "n": 16,
                 "r_on_mean": 1e4,
                 "r_on_stddev": 1e3,
                 "r_off_mean": 1e5,
                 "r_off_stddev": 1e4,
                 "dac_resolution": 4,
                 "adc_resolution": 14,
                 "device_resolution": 4,
                 "bias_scheme": 1/3,
                 "tile_rows": 4,
                 "tile_cols": 4,
                 "r_cmos_line": 600,
                 "r_cmos_transistor": 20, 
                 "p_stuck_on": 0.01,
                 "p_stuck_off": 0.01}


def print_mapping(tensors, mapping, crossbar_size):
    cb = torch.zeros(*crossbar_size)
    for t, m in zip(tensors, mapping):
        cb[m[0]:m[0]+m[2], m[1]:m[1]+m[3]] = t
    rows = torch.nonzero(cb, as_tuple=True)[0].tolist()
    cols = torch.nonzero(cb, as_tuple=True)[1].tolist()
    values = cb[torch.nonzero(cb, as_tuple=True)].tolist()
    for val in zip(rows,cols,values):
        print(val[0], val[1], val[2], sep=', ')

"""3 mm - 300 ohm"""
"""

crb = crossbar(device_params)

A = torch.Tensor([[1,0],
                  [0,4]]).view(-1,2)

b = torch.Tensor([1,1]).view(-1,1)


A = torch.ones(8,4)
b = torch.ones(8,1)


print("output", crb.vmm(b,A) / 1.79345)
print(b.view(1,-1).mm(A))
"""
