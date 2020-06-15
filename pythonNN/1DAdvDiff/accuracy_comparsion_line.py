#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Created on Tue Mar 24 10:38:06 2020

@author: wenqianchen
"""

import sys
sys.path.insert(0,'../tools')
sys.path.insert(0,'../tools/NNs')
from AdvDiff1D import Net1DAdvDiff, Eqs1D, DEVICE
from Normalization import Normalization
import numpy as np
import torch

resultsdir = 'results_Equation_epoch20000_net30'
resultsdir = 'results_interpolate_epoch20000_net30'
resultsdir = 'results_NormalInterpolate_epoch20000_net30'
resultsdir = 'results_NormalEquation_epoch20000_net30'


matfile = 'problem1D.mat'
Mchooses = np.array(list(range(5,6,1)))
Mchooses = Mchooses[0:]
Nettypes = ['Label', 'Resi']
Nettypes = Nettypes[0:2]

roeqs = Eqs1D(matfile, 10)
alpha1 =np.linspace(roeqs.design_space[0,0],roeqs.design_space[1,0],2)
alpha2 = np.linspace(roeqs.design_space[0,1],roeqs.design_space[1,1],2)
alpha1, alpha2 = np.meshgrid(alpha1,alpha2);
alpha = np.stack((alpha1,alpha2), axis=2).reshape(-1, 2)

Error=np.zeros((Mchooses.shape[0], 3))
import matplotlib.pyplot as plt
plt.figure(figsize=(8,8));
for i in range(Mchooses.shape[0]):
    Mchoose = Mchooses[i]
    roeqs = Eqs1D(matfile, Mchoose)
    phi_Exact = roeqs.phix(roeqs.xgrid.T, alpha[:,0:1], alpha[:,1:2]) 
    plt.plot(roeqs.xgrid, phi_Exact.T, '-')
    # POD-G
    lamda_G = roeqs.POD_G(Mchoose, alpha)
    Error[i,0] = roeqs.GetError(alpha,lamda_G)

    
    for Nettype in Nettypes:
        #layers = [2, *[20]*3, Mchoose]
        netfile = resultsdir+'/'+Nettype+'%d'%(Mchoose)+'.net'
        Net =Net1DAdvDiff(layers=None, oldnetfile=netfile, roeqs=roeqs).to(DEVICE)
        Net.loadnet(netfile)
        
        # POD_NN
        if Nettype == 'Label':
            lamda_NN = Net(torch.tensor(alpha).float().to(DEVICE))
            Error[i,1] = roeqs.GetError(alpha, lamda_NN) 
        # POD-PINN
        elif Nettype == 'Resi':
            lamda_PINN = Net(torch.tensor(alpha).float().to(DEVICE))
            Error[i,2] = roeqs.GetError(alpha,lamda_PINN) 
        
    phi_num = np.matmul(lamda_G, roeqs.Modes.T)
    plt.plot(roeqs.xgrid, phi_num.T, '*')
# visualization

#plt.close('all')
#plt.figure(figsize=(6,6))
#plt.semilogy(Mchooses,Error[:,0], 'r-*',label='POD-G')
#plt.semilogy(Mchooses,Error[:,1], 'go',label='POD-NN')
#plt.semilogy(Mchooses,Error[:,2], 'yp',label='POD-PINN')
#plt.xlabel('M')
#plt.ylabel('Error')
#plt.title(resultsdir)
#plt.legend()
plt.show()