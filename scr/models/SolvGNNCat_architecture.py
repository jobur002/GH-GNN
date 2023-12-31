'''
Project: GNN_IAC_T
                    SolvGNNCat
                    
Author: Edgar Sanchez
-------------------------------------------------------------------------------
'''
import torch
import torch.nn as nn
import torch.optim
import torch.utils.data
import torch.nn.functional as F
from torch_geometric.nn import global_mean_pool
from torch_geometric.data import Data
import torch_geometric.nn as gnn
import numpy as np

class MPNNconv(nn.Module):
    def __init__(self, node_in_feats, edge_in_feats, node_out_feats,
                 edge_hidden_feats=32, num_step_message_passing=1):
        super(MPNNconv, self).__init__()

        self.project_node_feats = nn.Sequential(
            nn.Linear(node_in_feats, node_out_feats),
            nn.ReLU()
        )
        self.num_step_message_passing = num_step_message_passing
        
        edge_network = nn.Sequential(
            nn.Linear(edge_in_feats, edge_hidden_feats),
            nn.ReLU(),
            nn.Linear(edge_hidden_feats, node_out_feats*node_out_feats)
        )
        self.gnn_layer = gnn.NNConv(
            node_out_feats,
            node_out_feats,
            edge_network,
            aggr='add'
        )
        self.gru = nn.GRU(node_out_feats, node_out_feats)

    def reset_parameters(self):
        self.project_node_feats[0].reset_parameters()
        self.gnn_layer.reset_parameters()
        for layer in self.gnn_layer.edge_func:
            if isinstance(layer, nn.Linear):
                layer.reset_parameters()
        self.gru.reset_parameters()
        
    def forward(self, system_graph):
        
        node_feats = system_graph.x
        edge_index = system_graph.edge_index
        edge_feats = system_graph.edge_attr
        
        node_feats = self.project_node_feats(node_feats) # (V, node_out_feats)
        hidden_feats = node_feats.unsqueeze(0)           # (1, V, node_out_feats)

        for _ in range(self.num_step_message_passing):
            if torch.cuda.is_available():
                node_feats = F.relu(self.gnn_layer(x=node_feats.type(torch.FloatTensor).cuda(), 
                                                   edge_index=edge_index.type(torch.LongTensor).cuda(),
                                                   edge_attr=edge_feats.type(torch.FloatTensor).cuda()))
            else:
                node_feats = F.relu(self.gnn_layer(x=node_feats.type(torch.FloatTensor), 
                                                   edge_index=edge_index.type(torch.LongTensor),
                                                   edge_attr=edge_feats.type(torch.FloatTensor)))
            node_feats, hidden_feats = self.gru(node_feats.unsqueeze(0), hidden_feats)
            node_feats = node_feats.squeeze(0)
        return node_feats


class SolvGNNCat(nn.Module):
    def __init__(self, in_dim, hidden_dim, n_classes=1):
        super(SolvGNNCat, self).__init__()
        
        self.conv1 = gnn.GCNConv(in_dim, hidden_dim)
        self.conv2 = gnn.GCNConv(hidden_dim, hidden_dim)
        
        self.pool = global_mean_pool
        
        
        self.global_conv1 = MPNNconv(node_in_feats=hidden_dim+3,
                                     edge_in_feats=1,
                                     node_out_feats=hidden_dim)
        
        # MLP unique
        self.mlp1_unique = nn.Linear(hidden_dim*2+1, hidden_dim*2)
        self.mlp2_unique = nn.Linear(hidden_dim*2, hidden_dim)
        self.mlp3_unique = nn.Linear(hidden_dim, 1)
        
        
    def generate_sys_graph(self, x, edge_attr, batch_size, n_mols=2):
        
        src = np.arange(batch_size)
        dst = np.arange(batch_size, n_mols*batch_size)
        
        # Self-connections (within same molecule)
        self_connection = np.arange(n_mols*batch_size)
        
        # Biderectional connections (between each molecule in the system)
        # and self-connection
        one_way = np.concatenate((src, dst, self_connection))
        other_way = np.concatenate((dst, src, self_connection))
        
        edge_index = torch.tensor([list(one_way),
                                   list(other_way)], dtype=torch.long)
        sys_graph = Data(x=x, edge_index=edge_index, edge_attr=edge_attr)
        return sys_graph

    def forward(self, solvent, solute, T):
        batch_size = solvent.y.shape[0]
        # Molecular descriptors based on MOSCED model
        
        # -- Induction via polarizability 
        # -- (i.e., dipole-induced dipole or induced dipole - induced dipole)
        
        # ---- Atomic polarizability
        ap1 = solvent.ap.reshape(-1,1)
        ap2 = solvent.ap.reshape(-1,1)
        
        # ---- Bond polarizability
        bp1 = solvent.bp.reshape(-1,1)
        bp2 = solvent.bp.reshape(-1,1)
        
        # -- Polarity via topological polar surface area
        topopsa1 = solvent.topopsa.reshape(-1,1)
        topopsa2 = solute.topopsa.reshape(-1,1)
        
        # -- Hydrogen-bond acidity and basicity
        intra_hb1 = solvent.inter_hb
        intra_hb2 = solute.inter_hb
        
        u1 = torch.cat((ap1,bp1,topopsa1), axis=1) # Molecular descriptors solvent
        u2 = torch.cat((ap2,bp2,topopsa2), axis=1) # Molecular descriptors solute
        
        # Intermolecular descriptors
        # -- Hydrogen bonding 
        inter_hb  = solvent.inter_hb
        
                
        x1_temp = F.relu(self.conv1(solvent.x, solvent.edge_index))
        x1_temp = F.relu(self.conv2(x1_temp, solvent.edge_index))
        
        x2_temp = F.relu(self.conv1(solute.x, solute.edge_index))
        x2_temp = F.relu(self.conv2(x2_temp, solute.edge_index))
        
        xg1 = self.pool(x1_temp, solvent.batch)
        xg2 = self.pool(x2_temp, solute.batch)
        
        # Construct binary system graph
        node_feat = torch.cat((
            torch.cat((xg1, u1), axis=1),
            torch.cat((xg2, u2), axis=1)
            ),axis=0)
        edge_feat = torch.cat((inter_hb.repeat(2),
                             intra_hb1,
                             intra_hb2)).unsqueeze(1)
        
        
        binary_sys_graph = self.generate_sys_graph(x=node_feat,
                                                   edge_attr=edge_feat,
                                                   batch_size=batch_size)
        
        # Binary system fingerprint
        xg = self.global_conv1(binary_sys_graph)
        xg = torch.cat((xg[0:len(xg)//2,:], xg[len(xg)//2:,:]), axis=1)
        
        T = T.x.reshape(-1,1) + 273.15
        
        T_min = -60 + 273.15
        T_max = 289.3 + 273.15
        T_norm = (T-T_min)/(T_max-T_min)
        
        xg = torch.cat((xg, T_norm), axis=1)
        
        output = F.relu(self.mlp1_unique(xg))
        output = F.relu(self.mlp2_unique(output))
        output = self.mlp3_unique(output)
        
        return output    


def count_parameters(model):
    return sum(p.numel() for p in model.parameters() if p.requires_grad)

