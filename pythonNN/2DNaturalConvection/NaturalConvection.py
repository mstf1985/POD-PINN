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
2D Natural convection in enclosed cavity Problem:
    >  u_x + v_y = 0
    >  u*u_x + v*u_y = -p_x + sqrt(Pr/Ra)*(u2_x2 + u2_y2) + T sinTh
    >  u*v_x + v*v_y = -p_y + sqrt(Pr/Ra)*(v2_x2 + v2_y2) + T*cosTh
    >  u*T_x + v*T_y = 1/sqrt(Pr*Ra)*(T2_x2 + T2_y2) 
    > where alpha = (Pr, Ra, Th) in [100, 1000]x[pi/6,5pi/6] is design parameters
Reduced order equations:
    > p_Modes' * Eq1 + u_Modes' * Eq2 +v_Modes * Eq3 + T_Modes * Eq4 = 0
    
Created on Wed May 4 21:25:38 2020

@author: wenqianchen
"""



import sys
sys.path.insert(0,'../tools')
sys.path.insert(0,'../tools/NNs')

from Chebyshev import Chebyshev2D
from scipy.io import loadmat
import numpy as np
import torch
from NN import POD_Net, DEVICE
from Normalization import Normalization
from scipy.optimize import fsolve, root

# Eqs parameters
NVAR = 4     # the number of unknown variables: p,u,v,T
NVARLOAD = 6 # the number of loaded variables: p,u,v,T,omega,psi
Newton = {'iterMax':100, 'eps':1E-6}

# reproducible
torch.manual_seed(1234)  
np.random.seed(1234)


class CustomedEqs():    
    def __init__(self, matfilePOD,PODNum,matfileValidation, M):
        datas = loadmat(matfilePOD)
        # data for POD
        self.Samples      = datas['Samples'][:,0:PODNum]
        self.FieldShape   = tuple(datas['FieldShape'][0])
        self.parameters   = datas['parameters'][0:PODNum,:]
        self.design_space = datas['design_space']
        self.NSample = self.Samples.shape[1]
        
        # data for validation
        datas = loadmat(matfileValidation)
        self.ValidationParameters   = datas['parameters']
        self.ValidationSamples      = self.ExtractInteriorSnapshots( datas['Samples'] )
    
        
        # svd decomposition
        self.Modes, self.sigma, _ = np.linalg.svd( self.ExtractInteriorSnapshots(self.Samples) );
        self.Modes = self.Modes[:,:M]
        self.M = M
        
        # spatial discretization
        self.xCoef, self.yCoef = 1/2, 1/2
        self.Chby2D   = Chebyshev2D(xL=-1, xR=1, yD=-1, yU=1, Mx=self.FieldShape[0]-1,My=self.FieldShape[1]-1)
        self.dxp,self.dyp  = self.Chby2D.DxCoeffN2()
        self.dx, self.dy   = self.Chby2D.DxCoeff(1) 
        self.d2x, self.d2y = self.Chby2D.DxCoeff(2)

        # projections
        self.projections = np.matmul( self.Modes.T, self.ExtractInteriorSnapshots(self.Samples))
        _, Mapping  = Normalization.Mapstatic(self.projections.T)
        self.proj_mean =  Mapping[0][None,:] 
        self.proj_std  =  Mapping[1][None,:] 
        
        
        # reduced-order equations
        self.InteriorShape = (self.FieldShape[0]-2, self.FieldShape[1]-2,)
        self.Interior = np.zeros(self.FieldShape)
        self.Interior[1:-1,1:-1]=1
        #self.Interior[1:(self.FieldShape[0]+1)//2, :                         ] =1
        #self.Interior[  (self.FieldShape[0]-1)//2, :                         ] *=0.5
        
        
        self.Boundary = np.ones(self.FieldShape); self.Boundary[1:-1,1:-1]=0
        self.TBC = np.reshape(self.Samples[3::NVARLOAD,0], self.FieldShape)*self.Boundary
        self.TBC[1:-1,[0,-1]]=0;
        # compute T on y boundary to meet boundary condition dT_dy = 0
        TM = self.dy[0::self.InteriorShape[0]+1,0::self.InteriorShape[1]+1]
        self.invTM = np.linalg.inv(TM)
        self.Beqs, self.Bbc = self.getB()
        self.Aeqs, self.Abc = self.getA()

        # Compute projection error
        self.lamda_proj = np.matmul(self.ValidationSamples.T, self.Modes)
        self.ProjError = self.GetError(self.lamda_proj)
        
    def Mode2Field(self, Vec):
        p,u,v,T= np.zeros(self.FieldShape), np.zeros(self.FieldShape), np.zeros(self.FieldShape), np.zeros(self.FieldShape)
        p[1:-1,1:-1] = np.reshape( Vec[0::NVAR], self.InteriorShape)
        u[1:-1,1:-1] = np.reshape( Vec[1::NVAR], self.InteriorShape)
        v[1:-1,1:-1] = np.reshape( Vec[2::NVAR], self.InteriorShape)
        T[1:-1,1:-1] = np.reshape( Vec[3::NVAR], self.InteriorShape)
        # compute T on y boundary to meet boundary condition dT_dy = 0
        T[1:-1,[0,-1]] = np.matmul( self.invTM, -np.matmul( self.dy[[0,-1],1:-1],T[1:-1,1:-1].T ) ).T        
        return p,u,v,T
    def ExtractInteriorSnapshots(self,Samples):
        NSample =Samples.shape[1]
        Samples_shape = (self.FieldShape[0], self.FieldShape[1],NVARLOAD,NSample,)
        return np.reshape( np.reshape(Samples, Samples_shape)[1:-1, 1:-1, 0:NVAR, :], (-1, NSample))
    
    def Compute_d_dxc(self, phi):
        return np.matmul(self.dx,phi)/self.xCoef
    def Compute_d_dyc(self, phi):
        return np.matmul(self.dy, phi.T).T/self.yCoef
    def Compute_dp_dxc(self, phi):
        return np.matmul(self.dxp,phi)/self.xCoef
    def Compute_dp_dyc(self, phi):
        return np.matmul(self.dyp, phi.T).T/self.yCoef   
    def Compute_d_dxc2(self, phi):
        return self.Compute_d_dxc( self.Compute_d_dxc(phi) )
    def Compute_d_dyc2(self, phi):
        return self.Compute_d_dyc( self.Compute_d_dyc(phi) )
    def Compute_d_dxcyc(self, phi):
        return self.Compute_d_dyc( self.Compute_d_dxc(phi) )
    def Compute_d_d1(self, phi):
        return self.Compute_d_dxc(phi), self.Compute_d_dyc(phi)
    def Compute_d_d1p(self, phi):
        return self.Compute_dp_dxc(phi), self.Compute_dp_dyc(phi)
    def Compute_d_d2(self, phi):
        return self.Compute_d_dxc2(phi), self.Compute_d_dyc2(phi)
    
    # get A from the first mth modes
    def getA(self): 
        """0:3 namely first index is related to terms:
           index [                        0                         ]
           terms [ [uu_x+vu_y]   +   [uv_x+vv_y]  +   [uT_x+vT_y]   ]
           weight[                        1                         ]
           coeff [     u         ,        v       ,        T        ]
        """
        Aeqs = np.zeros((1,self.M, self.M, self.M))
        Abc  = np.zeros((1,self.M, self.M))
        TBCxc, TBCyc= self.Compute_d_d1(self.TBC)
        for j in range(self.M):
            pj, uj, vj, Tj= self.Mode2Field(self.Modes[:,j])
            ujxc, ujyc= self.Compute_d_d1(uj)      
            vjxc, vjyc= self.Compute_d_d1(vj) 
            Tjxc, Tjyc= self.Compute_d_d1(Tj) 
            for k in range(self.M):
                pk, uk,vk,Tk= self.Mode2Field(self.Modes[:,k])
                for i in range(self.M):
                    pi, ui,vi,Ti = self.Mode2Field(self.Modes[:,i])
                    Aeqs[0,k,i,j] = ( self.Interior*(ui*ujxc*uk + vi*ujyc*uk) ).sum() \
                                   +( self.Interior*(ui*vjxc*vk + vi*vjyc*vk) ).sum() \
                                   +( self.Interior*(ui*Tjxc*Tk + vi*Tjyc*Tk) ).sum()
                    Abc[0,k,i]    = ( self.Interior*(ui*TBCxc*Tk + vi*TBCyc*Tk) ).sum()
        return Aeqs,Abc
        
    def getB(self):
        """0:10 namely first index is related to terms:    
           index  [                 0                     1          2        3            4   ]
           terms  [    -[u_xx+u_yy] - [v_xx+v_yy]   -[T_xx+T_yy]    -T       -T       [u_x +v_y+p_x+p_y]]
           weight [          u      +      v              T          u        v         p  + p + u + v  ]
           coeff  [              sqrt(Pr/Ra)        1/sqrt(Pr*Ra) sin(Th)  cos(Th)         1   ]
        """
        
        Beqs = np.zeros((5,self.M, self.M))
        Bbc  = np.zeros((5,self.M))
        
        TBCxc,   TBCyc = self.Compute_d_d1(self.TBC)     
        TBCxc2, TBCyc2 = self.Compute_d_d2(self.TBC)  
        for j in range(self.M):
            pj, uj, vj, Tj= self.Mode2Field(self.Modes[:,j])      
            ujxc, ujyc= self.Compute_d_d1(uj)
            vjxc, vjyc= self.Compute_d_d1(vj)
            Tjxc, Tjyc= self.Compute_d_d1(Tj)
            pjxc, pjyc= self.Compute_d_d1p(pj)
            ujxc2, ujyc2 = self.Compute_d_d2(uj)
            vjxc2, vjyc2 = self.Compute_d_d2(vj)
            Tjxc2, Tjyc2 = self.Compute_d_d2(Tj)
            for i in range(self.M):
                pi, ui,vi,Ti = self.Mode2Field(self.Modes[:,i])
                Beqs[0,i,j] =-( self.Interior*(  ujxc2*ui + ujyc2*ui ) ).sum()\
                             -( self.Interior*(  vjxc2*vi + vjyc2*vi ) ).sum()
                Beqs[1,i,j] =-( self.Interior*(  Tjxc2*Ti + Tjyc2*Ti ) ).sum()
                Beqs[2,i,j] =-( self.Interior*(  Tj   *ui            ) ).sum()
                Beqs[3,i,j] =-( self.Interior*(  Tj   *vi            ) ).sum()
                Beqs[4,i,j] = ( self.Interior*(  ujxc*pi  +  vjyc*pi ) ).sum()\
                             +( self.Interior*(  pjxc*ui  +  pjyc*vi ) ).sum()

                Bbc[0,i] = 0
                Bbc[1,i] =-( self.Interior*(  TBCxc2*Ti + TBCyc2*Ti ) ).sum()
                Bbc[2,i] = 0
                Bbc[3,i] = 0
                Bbc[4,i] = 0              
        return Beqs,Bbc
    
    def getABCoef(self, alpha, cos=np.cos, sin=np.sin, sqrt=np.sqrt, cat=np.concatenate ):
        
        Ra    = alpha[:,0:1]
        Pr    = alpha[:,1:2]
        Theta = alpha[:,2:3]/180*3.141592653589793
        one   = Ra*0 + 1
        Acoef = one
        BCoef = cat((sqrt(Pr/Ra), 1/sqrt(Pr*Ra), sin(Theta), cos(Theta), one), axis=1)
        return Acoef, BCoef
        
    def POD_Gfsolve(self,alpha, lamda_init= None):
        n = alpha.shape[0]
        lamda  = np.zeros((n, self.M))
        def compute_eAe(A, e):
            tmp  = np.matmul(e.T, A)
            return np.matmul(tmp, e).squeeze(axis=(2))
        def eqs(x,A,B,source):
            lamda = x[:,None];
            lamda = lamda*self.proj_std.T + self.proj_mean.T
            err = compute_eAe(A,lamda) + np.matmul(B,lamda) -source
            return err.squeeze()
        for i in range(n):
            alphai = alpha[i:i+1,0:3]
            AiCoeff, BiCoeff = self.getABCoef(alphai)
            AiCoeff, BiCoeff = AiCoeff.squeeze(axis=0), BiCoeff.squeeze(axis=0)
            Ai = ( AiCoeff[:,None,None,None]* self.Aeqs ).sum(axis=0)
            Bi = ( AiCoeff[:,None,None]* self.Abc  ).sum(axis=0) \
                +( BiCoeff[:,None,None]* self.Beqs ).sum(axis=0)
            sourcei = -( BiCoeff[:,None]* self.Bbc  ).sum(axis=0)[:,None]
            
            if lamda_init is None:
                dis = (alphai - self.parameters)/ (self.design_space[1:2,:]-self.design_space[0:1,:] )
                dis = np.linalg.norm(dis, axis=1);
                ind = np.where(dis == dis.min())[0][0]
                lamda0 = self.projections[0:self.M, ind:ind+1].T
            else:
                lamda0 = lamda_init[i:i+1,:]   
            lamda0 = (lamda0-self.proj_mean)/self.proj_std
            lamdasol = fsolve(lambda x: eqs(x,Ai,Bi,sourcei), lamda0.squeeze())
            err = np.linalg.norm( eqs(lamdasol, Ai, Bi, sourcei) )
            if err > Newton["eps"]:
                print('Case (%d) can only reach to an error of %f'%(i, err))
                #print('Case (%f,%f,%f) can only reach to an error of %f'%(alphai[0,0], alphai[0,1], alphai[0,2], err))
                #lamdasol = lamdasol*0 + np.inf
            lamda[i,:] = lamdasol[None,:]*self.proj_std + self.proj_mean
        return lamda
    
    
    def GetError(self,lamda):
        Nvalidation =self.ValidationParameters.shape[0]
        if  Nvalidation != lamda.shape[0]:
            raise Exception('The number of lamda should be equal to validation parameters')
        phi_pred         = np.matmul( lamda, self.Modes.T)
        phi_Num          = self.ValidationSamples.T
        Error = np.zeros((Nvalidation,NVAR))    # the second dimension is [p,u,v]
        for nvar in range(NVAR):
            Error[:,nvar] = np.linalg.norm(phi_Num[:,nvar::NVAR]-phi_pred[:,nvar::NVAR], axis = 1)\
                           /np.linalg.norm(phi_Num[:,nvar::NVAR], axis=1)
        ErrorpuvT = Error.mean(axis=0)
        Errortotal =  np.linalg.norm(phi_Num[:,:]-phi_pred[:,:], axis = 1)\
                                   /np.linalg.norm(phi_Num[:,:], axis=1)
#        for i in range(Nvalidation):
#            print(i,'%e'%Errortotal[i])
        Errortotal = Errortotal.mean(axis=0)
        print("Errors=[%f,%f,%f,%f],%f"%(ErrorpuvT[0],ErrorpuvT[1],ErrorpuvT[2],ErrorpuvT[3], Errortotal))
        return ErrorpuvT, Errortotal
        
    def getGrid(self,alpha,cos=np.cos, sin=np.sin):
        xc,yc = self.Chby2D.grid()
        Theta = alpha[:,2:3]/180*3.14159265359 
        xp = xc*self.xCoef*cos(Theta) - yc*self.yCoef*sin(Theta)
        yp = xc*self.xCoef*sin(Theta) + yc*self.yCoef*cos(Theta)
        return xp,yp,xc,yc
    
    def GetPredFields(self,alpha,lamda, filename):
        Ncase = lamda.shape[0]
        Fields = []
        phi_pred  = np.matmul( lamda, self.Modes.T)
        for icase in range(Ncase):
            alphai = alpha[icase:icase+1,:]
            pi,ui,vi,Ti= self.Mode2Field(phi_pred[icase,:])
            Ti = Ti +self.TBC
            ## compute vorticity and streamfunction
            _, ui_yc= self.Compute_d_d1(ui)
            vi_xc, _= self.Compute_d_d1(vi)
            xp,yp,xc,yc = self.getGrid(alphai)
            hx = abs(xc[0,0]-xc[1,0])*self.xCoef
            hy = abs(yc[0,0]-yc[0,1])*self.yCoef
            omegai =  ui_yc -vi_xc
            ## solve psi with explicit method
            # psi_xp2 + psi_yp2 = omega
            dt = 0.5*min(hx,hy)**2
            psii = 0*omegai
            for it in range(int(1E8)):
                psi_xc2,psi_yc2 = self.Compute_d_d2(psii) 
                dpsi = psi_xc2+psi_yc2-omegai
                dpsi = dpsi * self.Interior
                psii = psii +dpsi*dt
                if it%10000==0:
                    print('%8d, dpsi=%e'%(it, np.abs(dpsi).max()))
                if np.abs(dpsi).max() < 1E-8:
                    break
            
            # write result
            Nx,Ny = self.FieldShape
            with open(filename+'%d'%icase+'.plt','w') as f:
                header = """
title="result"
variables="x","y","P","u","v","omega","psi"
zone,j=%d, i=%d,f=point"""%(Ny,Nx) + "\n"
                f.write(header)
                for j in range(Ny):
                    for i in range(Nx):
                        line=("%21.16f\t"*8 + "\n" )%(xp[i,j],yp[i,j],pi[i,j],ui[i,j],vi[i,j],Ti[i,j],omegai[i,j],psii[i,j])
                        f.write(line)
            
            Fields.append( np.stack((xp, yp, pi, ui, vi, Ti, omegai, psii), axis=0) )
        Fields = np.stack( tuple(Fields), axis=0)
        from scipy.io import savemat
        savemat(filename+'.mat', {'Fields':Fields})
        return Fields

    
    


    
class CustomedNet(POD_Net):
    def __init__(self, layers=None,oldnetfile=None,roeqs=None):
        super(CustomedNet, self).__init__(layers=layers,OldNetfile=oldnetfile)
        self.M = roeqs.M
        self.Aeqs = torch.tensor( roeqs.Aeqs ).float().to(DEVICE)
        self.Abc  = torch.tensor( roeqs.Abc  ).float().to(DEVICE)
        self.Beqs = torch.tensor( roeqs.Beqs ).float().to(DEVICE)
        self.Bbc  = torch.tensor( roeqs.Bbc  ).float().to(DEVICE)
        self.lb   = torch.tensor(roeqs.design_space[0:1,:]).float().to(DEVICE)
        self.ub   = torch.tensor(roeqs.design_space[1:2,:]).float().to(DEVICE)
        self.proj_std = torch.tensor( roeqs.proj_std ).float().to(DEVICE)
        self.proj_mean= torch.tensor( roeqs.proj_mean).float().to(DEVICE)
        self.roeqs = roeqs
        
        self.labeled_inputs  = torch.tensor( roeqs.parameters ).float().to(DEVICE)
        self.labeled_outputs = torch.tensor( roeqs.projections.T ).float().to(DEVICE)
        self.labeledLoss = self.loss_Eqs(self.labeled_inputs,self.labeled_outputs )
        print('labelloss=',self.labeledLoss)
        pass
        
    def u_net(self,x):
        x = (x-(self.ub+self.lb)/2)/(self.ub-self.lb)*2
        out = self.unet(x)
        out = out*self.proj_std + self.proj_mean
        return out
    
    def forward(self,x):
        return self.u_net(x).detach().cpu().numpy()
    
    def loss_NN(self, xlabel, ylabel):
        y_pred    = self.u_net(xlabel)
        loss_NN   = self.lossfun(ylabel/self.proj_std, \
                                 y_pred/self.proj_std )
        return loss_NN
    
    def loss_PINN(self,x,dummy=None,weight=1):
        return self.loss_Eqs(x,self.u_net(x),weight)
    
    def loss_Eqs(self,x,lamda,weight=1):
        #lamda = self.u_net(x);
        ACoeff, BCoeff = self.roeqs.getABCoef(x, cos=torch.cos, sqrt=torch.sqrt, sin=torch.sin, cat=torch.cat)
        A = self.Aeqs
        B = self.Abc \
           +( BCoeff[:,:,None,None]* self.Beqs[None,:,:,:]  ).sum(axis=1)
        source = -( BCoeff[:,:,None]* self.Bbc[None,:,:]  ).sum(axis=1)
        fx   = torch.matmul(lamda[:,None,None,:], A)
        fx   = torch.matmul(fx,lamda[:,None,:,None])
        fx   = fx.view(lamda.shape) + torch.matmul(B,lamda[:,:,None]).view(lamda.shape) -source
        return self.lossfun(weight*fx,torch.zeros_like(fx))
        
if __name__ == '__main__':
    design_space = np.array([[1E5,0.6,0],[1E6,0.8,180]])
    design_space = np.array([[1E5,0.6,0],[3E5,0.8,90]])
    design_space = np.array([[1E5,0.6,0],[5E5,0.8,90]])
    design_space = np.array([[1E5,0.6,60],[5E5,0.8,90]])
    design_space = np.array([[1E4,0.6,45],[1E5,0.8,90]])
    root='%0.0E_%0.0Eand%0.2f_%0.2fand%d_%d'%(design_space[0,0],design_space[1,0], \
                              design_space[0,1],design_space[1,1], \
                              design_space[0,2],design_space[1,2],)
    #root = 'others'
    NumSolsdir = 'NumSols/'+root
    matfilePOD = NumSolsdir  + '/'+'NaturalConvectionPOD.mat'
    matfileValidation = NumSolsdir  + '/'+'NaturalConvectionValidation.mat'
    M = 10
    roeqs = CustomedEqs(matfilePOD, 100, matfileValidation, M)
    import matplotlib.pyplot as plt
    plt.figure(figsize=(6,6))
    plt.semilogy(np.arange(roeqs.sigma.shape[0])+1, roeqs.sigma )
    plt.xlabel('$M$')
    plt.ylabel('Singular value')    
    plt.show()
    plt.savefig('fig/'+root+'.png')
#    
#    alpha = roeqs.ValidationParameters
#    lamda = roeqs.POD_Gfsolve(alpha, roeqs.lamda_proj)
#    #lamda= roeqs.POD_GNewton(alpha)
#    ErrorpuvT,Errortotal = roeqs.GetError(lamda)
#    print("Error=",ErrorpuvT, Errortotal)
    
    Net = CustomedNet(roeqs=roeqs,layers=[3,20,20,20,M])
    print(Net.labeledLoss)
    
#    Resi_inputs  = roeqs.ValidationParameters
#    dummy = np.zeros((Resi_inputs.shape[0],roeqs.M))
#    data = (Resi_inputs, dummy, 'Resi',0.9,)
#    from NN import train, train_options_default
#    options = train_options_default.copy()
#    options['weight_decay']=0
#    options['NBATCH'] = 10
#    train(Net,data,'tmp.net',options=options)
    
    # VALIDATE
    for ind in range(roeqs.NSample):
    #ind = 1
        Ra=roeqs.parameters[ind,0]
        Pr=roeqs.parameters[ind,1]
        Theta=roeqs.parameters[ind,2]/180*np.pi
        x,y = roeqs.Chby2D.grid()
        
#        Vec = roeqs.ExtractInteriorSnapshots(roeqs.Samples[:,ind:ind+1]).squeeze()
#        pj,uj,vj,Tj = roeqs.Mode2Field(Vec)
#        Tj = Tj + roeqs.TBC
        
        Vec  = roeqs.Samples[ :,ind]
        pj = np.reshape( Vec[0::NVARLOAD], roeqs.FieldShape)
        uj = np.reshape( Vec[1::NVARLOAD], roeqs.FieldShape)
        vj = np.reshape( Vec[2::NVARLOAD], roeqs.FieldShape)
        Tj = np.reshape( Vec[3::NVARLOAD], roeqs.FieldShape)
        
        ujxc, ujyc= roeqs.Compute_d_d1(uj)
        vjxc, vjyc= roeqs.Compute_d_d1(vj)
        Tjxc, Tjyc= roeqs.Compute_d_d1(Tj)
        pjxc, pjyc= roeqs.Compute_d_d1p(pj)
        ujxc2, ujyc2 = roeqs.Compute_d_d2(uj)
        vjxc2, vjyc2 = roeqs.Compute_d_d2(vj)
        Tjxc2, Tjyc2 = roeqs.Compute_d_d2(Tj)
        
        eq1 = (ujxc + vjyc)
        eq2 =  uj*ujxc+vj*ujyc+pjxc-np.sqrt(Pr/Ra)*(ujxc2+ujyc2)-Tj*np.sin(Theta)
        eq3 =  uj*vjxc+vj*vjyc+pjyc-np.sqrt(Pr/Ra)*(vjxc2+vjyc2)-Tj*np.cos(Theta)
        eq4 =  uj*Tjxc+vj*Tjyc     -1/np.sqrt(Pr*Ra)*(Tjxc2+Tjyc2)
        eq1 =  eq1*roeqs.Interior
        eq2 =  eq2*roeqs.Interior
        eq3 =  eq3*roeqs.Interior
        eq4 =  eq4*roeqs.Interior
        #print('%d: (%e, %e, %e, %e)'%(ind, abs(eq1).max(), abs(eq2).max(),abs(eq3).max(), abs(eq4).max(), ))
        print('%d: (%e, %e, %e, %e)'%(ind, np.sqrt((eq1**2).mean()),\
                                           np.sqrt((eq2**2).mean()),\
                                           np.sqrt((eq3**2).mean()),\
                                           np.sqrt((eq4**2).mean()), ))
        