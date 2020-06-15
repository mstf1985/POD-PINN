#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
@POD based reduced order model
Used for solving the reduced-order equation generated by POD basis method
including POD-G   :: solve the equation with online Newton-like iteration method
          POD-NN  :: solve the equation with offline  NN trained only with sample points 
          POD-PINN:: solve the euuation with offline  NN trained with sample points and equation
For general use, the reduced equations are simplified as the following formulation:
          alpha' * A * alpha + B * alpha = f
          where A B C and f are all functions of design parameters
          
@ Customed problem
1D viscous Burges Equations:
    > phi*phi_x - a*phi_xx = f
    > where V=a=1
    * the artifical solutions are defined as 
    > phi = sin(-alpha2*x/3).*(1+alpha1*x)*(x^2-1)
    > where alpha = (alpha1, alpha2) in [1, 10]x[1,10] is design parameters
Reduced order equations:
    > phi_Modes‘*(V*Dx-a*D2x)*phi_Modes*lamda = phi_Modes'*f
    


Created on Wed Mar 18 14:40:38 2020

@author: wenqianchen
"""



import sys
sys.path.insert(0,'../tools')
sys.path.insert(0,'../tools/NN')

from Chebyshev import Chebyshev1D
from scipy.io import loadmat
import numpy as np
import torch
import torch.autograd as ag
from NN import POD_Net, DEVICE
from Normalization import Normalization

# Eqs parameters
a  = 1
Newton = {'iterMax':100, 'eps':1E-10}

# reproducible
torch.manual_seed(1234)  
np.random.seed(1234)


class CustomedEqs():    
    def __init__(self, matfile, M):
        datas = loadmat(matfile)
        self.Samples = datas['Samples']
        self.xgrid   = datas['xgrid']
        self.parameters = datas['parameters']
        self.design_space = datas['design_space']
        
        
        self.Np      = self.Samples.shape[0]-1
        self.NSample = self.Samples.shape[1]
        
        # svd decomposition
        self.Modes, self.sigma, _ = np.linalg.svd(self.Samples);
        self.Modes = self.Modes[:,:M]
        self.M = M
        
        
        
        # spatial discretization
        Cheb1D  = Chebyshev1D(self.xgrid[0], self.xgrid[-1], self.Np)
        self.dx      = Cheb1D.DxCoeff();
        self.d2x     = Cheb1D.DxCoeff(2);
        self.projections = np.matmul( self.Modes.T, self.Samples)
        _, Mapping  = Normalization.Mapstatic(self.projections.T)
        self.proj_mean =  Mapping[0][None,:] 
        self.proj_std  =  Mapping[1][None,:] 
        
    # get A from the first mth modes
    def getA(self): 
        V_x = np.matmul(self.dx, self.Modes);  
        A = np.matmul( self.Modes.reshape((-1,self.M,1)), V_x.reshape(-1,1,self.M))
        A[0,:,:] = 0; A[-1,:,:]=0;
        A = A[None,:]*self.Modes.T.reshape((self.M, -1,1,1))
        A = A.sum(axis=1).squeeze().reshape((self.M, self.M, self.M))

        return A        
        
    def getB(self):
        tmp =-a*self.d2x
        # add boundary conditions
        tmp[0,:] =0; tmp[-1, :]=0;
        tmp[0,0] =1; tmp[-1,-1]=1;
        
        tmp = np.matmul(self.Modes.T, tmp)
        B = np.matmul(tmp,self.Modes)
        return B
    
    def POD_G(self,Mchoose, alpha):
        alpha1 = alpha[:,0:1]
        alpha2 = alpha[:,1:2]
        n = alpha1.shape[0]
        lamda  = np.zeros((alpha.shape[0], self.M))
        def compute_eAe(A, e):
            tmp  = np.matmul(e.T, A)
            return np.matmul(tmp, e).squeeze(axis=(2))
        def compute_dA(A,e):
            return np.matmul(A+A.transpose((0, 2,1)), e).squeeze(axis=(2))
        
        A = self.getA()
        B = self.getB()
        for i in range(n):
            alpha1i = alpha1[i:i+1,0:1]; alpha2i = alpha2[i:i+1, 0:1];
            source = self.getsource(alpha1i, alpha2i).T
            dis = alpha[i:i+1,:] - self.parameters
            dis = np.linalg.norm(dis, axis=1);
            ind = np.where(dis == dis.min())[0][0]
            lamda0 = self.projections[0:self.M, ind:ind+1]
            #Newton iteration
            it = 0; err =1;
            while it<=Newton['iterMax'] and err>Newton['eps']:
                it +=1
                R0 = compute_eAe(A,lamda0) + np.matmul(B,lamda0) - source
                err = np.linalg.norm(R0)
                dR = compute_dA(A,lamda0) + B
                dlamda = -np.linalg.solve(dR, R0)
                lamda0 =lamda0 + dlamda
            if it>=Newton['iterMax']:
                print('Case (%f,%f) can only reach to an error of %f'%(alpha1[i,0], alpha2[i,0], err))
                lamda0 = lamda0*0 + np.inf
            lamda[i,:] = lamda0.squeeze()
        return lamda
    
    def GetError(self,alpha,lamda):
        alpha1 = alpha[:,0:1]
        alpha2 = alpha[:,1:2]
        phi_pred         = np.matmul( lamda, self.Modes.T)
        phi_Exact        = self.phix(self.xgrid.T, alpha1, alpha2)
        Error = np.linalg.norm(phi_Exact-phi_pred, axis = 1)/np.linalg.norm(phi_Exact, axis=1)
        Error = Error[None,:]
        Error = Error.mean()
        return Error
    def GetProjError(self, alpha):
        alpha1 = alpha[:,0:1]
        alpha2 = alpha[:,1:2]        
        phi_Proj = self.phix(self.xgrid.T, alpha1, alpha2)
        lamda_Proj = np.matmul( phi_Proj, self.Modes )
        return self.GetError(alpha,lamda_Proj)
        
    def getsource(self,alpha1, alpha2):
        x = self.xgrid.T
        f1    = (1+alpha1*x)*(x**2-1)
        f2    = np.sin(-alpha2*x/3)
        f1_x  = 3*alpha1*x**2 +2*x -alpha1
        f2_x  = -alpha2/3*np.cos(-alpha2*x/3)
        f1_xx = 6*alpha1*x + 2
        f2_xx = -alpha2**2/9*np.sin(-alpha2*x/3)
        
        phi_x  = f1*f2_x + f1_x*f2
        phi_xx = 2*f1_x*f2_x + f1*f2_xx +f1_xx*f2
        source = f1*f2*phi_x - a*phi_xx
        source[:, 0:1] =self.phix(x[0,  0], alpha1, alpha2)
        source[:,-1: ] =self.phix(x[0, -1], alpha1, alpha2)
        source = np.matmul( source, self.Modes )
        return source 
    
    def phix(self,x,alpha1,alpha2):
        return np.sin(-alpha2*x/3)*(1+alpha1*x)*(x**2-1)
    

    
class CustomedNet(POD_Net):
    def __init__(self, layers=None,oldnetfile=None,roeqs=None):
        super(CustomedNet, self).__init__(layers=layers,OldNetfile=oldnetfile)
        self.M = roeqs.M
        self.A = torch.tensor( roeqs.getA() ).float().to(DEVICE)
        self.B = torch.tensor( roeqs.getB() ).float().to(DEVICE)
        self.lb = torch.tensor(roeqs.design_space[0:1,:]).float().to(DEVICE)
        self.ub = torch.tensor(roeqs.design_space[1:2,:]).float().to(DEVICE)
        self.roeqs = roeqs
        self.proj_std = torch.tensor( roeqs.proj_std ).float().to(DEVICE)
        self.proj_mean= torch.tensor( roeqs.proj_mean).float().to(DEVICE)
        
        self.labeled_inputs  = torch.tensor( roeqs.parameters ).float().to(DEVICE)
        self.labeled_outputs = torch.tensor( roeqs.projections.T ).float().to(DEVICE)
        self.source = roeqs.getsource(roeqs.parameters[:,0:1], roeqs.parameters[:,1:2])
        self.source = torch.tensor( self.source ).float().to(DEVICE)
        self.labeledLoss = self.loss_Eqs(self.labeled_inputs,self.labeled_outputs, self.source)
        
    def u_net(self,x):
        x = (x-(self.ub+self.lb)/2)/(self.ub-self.lb)*2
        out = self.unet(x)
        out = out*self.proj_std + self.proj_mean
        return out
    
    def forward(self,x):
        return self.u_net(x).detach().cpu().numpy()
    
    def loss_NN(self, xlabel, ylabel):
        y_pred    = self.u_net(xlabel)
        diff = (ylabel-y_pred)/self.proj_std
        loss_NN   = self.lossfun(diff,torch.zeros_like(diff))
        return loss_NN
    
    def loss_PINN(self,x,source,weight=1):
        return self.loss_Eqs(x,self.u_net(x),source, weight)
        
    def loss_Eqs(self,x,lamda,source,weight=1):
#    def loss_PINN(self,x,source):
        #lamda = self.u_net(x);
        fx   = torch.matmul(lamda[:,None,None,:], self.A[None,:,:,:])
        fx   = torch.matmul(fx,lamda[:,None,:,None])
        fx   = fx.view(lamda.shape)
        fx   = fx + torch.matmul( lamda, self.B.T) -source
        return self.lossfun(weight*fx,torch.zeros_like(fx))
        
if __name__ == '__main__':
    NumSolsdir = 'NumSols'
    Nsample = 320
    matfile = NumSolsdir  + '/'+'Burges1D_SampleNum='+str(Nsample)+'.mat'
    M = 2
    roeqs = CustomedEqs(matfile, M)
    
#    Net = CustomedNet(roeqs=roeqs,layers=[2,20,20,20,M])
#    print(Net.labeledLoss)
        
    from plotting import newfig,savefig
    import matplotlib.pyplot as plt    
    newfig(width=1)
    plt.semilogy(np.arange(roeqs.sigma.shape[0])+1, roeqs.sigma,'-ko')
    plt.xlabel('$M$')
    plt.ylabel('Singular value')    
    plt.show()
    savefig('fig/SingularValues_%d'%(Nsample) )
    