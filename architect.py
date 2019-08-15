""" Architect controls architecture of cell by computing gradients of alphas """
import copy
import torch
from models.nas_modules import NASModule

class DARTSArchitect():
    """ Compute gradients of alphas """
    def __init__(self, net, w_momentum, w_weight_decay):
        """
        Args:
            net
            w_momentum: weights momentum
        """
        self.net = net
        self.v_net = copy.deepcopy(net)
        self.w_momentum = w_momentum
        self.w_weight_decay = w_weight_decay

    def virtual_step(self, trn_X, trn_y, xi, w_optim):
        """
        Compute unrolled weight w' (virtual step)

        Step process:
        1) forward
        2) calc loss
        3) compute gradient (by backprop)
        4) update gradient

        Args:
            xi: learning rate for virtual gradient step (same as weights lr)
            w_optim: weights optimizer
        """
        # forward & calc loss
        loss = self.net.loss(trn_X, trn_y) # L_trn(w)

        # compute gradient
        gradients = torch.autograd.grad(loss, self.net.weights())

        # do virtual step (update gradient)
        # below operations do not need gradient tracking
        with torch.no_grad():
            # dict key is not the value, but the pointer. So original network weight have to
            # be iterated also.
            for w, vw, g in zip(self.net.weights(), self.v_net.weights(), gradients):
                m = w_optim.state[w].get('momentum_buffer', 0.) * self.w_momentum
                vw.copy_(w - xi * (m + g + self.w_weight_decay*w))

            # synchronize alphas
            for a, va in zip(self.net.alphas(), self.v_net.alphas()):
                va.copy_(a)

    def step(self, trn_X, trn_y, val_X, val_y, xi, w_optim, a_optim):
        """ Compute unrolled loss and backward its gradients
        Args:
            xi: learning rate for virtual gradient step (same as net lr)
            w_optim: weights optimizer - for virtual step
        """
        a_optim.zero_grad()
        # do virtual step (calc w`)
        self.virtual_step(trn_X, trn_y, xi, w_optim)

        # calc unrolled loss
        loss = self.v_net.loss(val_X, val_y) # L_val(w`)

        # compute gradient
        v_alphas = tuple(self.v_net.alphas())
        v_weights = tuple(self.v_net.weights())
        v_grads = torch.autograd.grad(loss, v_alphas + v_weights)
        dalpha = v_grads[:len(v_alphas)]
        dw = v_grads[len(v_alphas):]

        hessian = self.compute_hessian(dw, trn_X, trn_y)

        # update final gradient = dalpha - xi*hessian
        with torch.no_grad():
            for alpha, da, h in zip(self.net.alphas(), dalpha, hessian):
                alpha.grad = da - xi*h
        a_optim.step()

    def compute_hessian(self, dw, trn_X, trn_y):
        """
        dw = dw` { L_val(w`, alpha) }
        w+ = w + eps * dw
        w- = w - eps * dw
        hessian = (dalpha { L_trn(w+, alpha) } - dalpha { L_trn(w-, alpha) }) / (2*eps)
        eps = 0.01 / ||dw||
        """
        norm = torch.cat([w.view(-1) for w in dw]).norm()
        eps = 0.01 / norm

        # w+ = w + eps*dw`
        with torch.no_grad():
            for p, d in zip(self.net.weights(), dw):
                p += eps * d
        loss = self.net.loss(trn_X, trn_y)
        dalpha_pos = torch.autograd.grad(loss, self.net.alphas()) # dalpha { L_trn(w+) }

        # w- = w - eps*dw`
        with torch.no_grad():
            for p, d in zip(self.net.weights(), dw):
                p -= 2. * eps * d
        loss = self.net.loss(trn_X, trn_y)
        dalpha_neg = torch.autograd.grad(loss, self.net.alphas()) # dalpha { L_trn(w-) }

        # recover w
        with torch.no_grad():
            for p, d in zip(self.net.weights(), dw):
                p += eps * d

        hessian = [(p-n) / 2.*eps for p, n in zip(dalpha_pos, dalpha_neg)]
        return hessian


class BinaryGateArchitect():
    """ Compute gradients of alphas """
    def __init__(self, net, w_momentum, w_weight_decay, n_samples=2, unrolled=False, renorm=True):
        """
        Args:
            net
            w_momentum: weights momentum
        """
        self.net = net
        self.w_momentum = w_momentum
        self.w_weight_decay = w_weight_decay
        self.n_samples = n_samples
        self.unrolled = unrolled
        self.renorm = renorm
        if unrolled:
            self.v_net = copy.deepcopy(net)
    
    def virtual_step(self, trn_X, trn_y, xi, w_optim):
        """
        Compute unrolled weight w' (virtual step)

        Step process:
        1) forward
        2) calc loss
        3) compute gradient (by backprop)
        4) update gradient

        Args:
            xi: learning rate for virtual gradient step (same as weights lr)
            w_optim: weights optimizer
        """
        # forward & calc loss
        loss = self.net.loss(trn_X, trn_y) # L_trn(w)

        # compute gradient
        gradients = torch.autograd.grad(loss, self.net.weights(check_grad=True))

        # do virtual step (update gradient)
        # below operations do not need gradient tracking
        with torch.no_grad():
            # dict key is not the value, but the pointer. So original network weight have to
            # be iterated also.
            for w, vw, g in zip(self.net.weights(check_grad=True), self.v_net.weights(check_grad=True), gradients):
                m = w_optim.state[w].get('momentum_buffer', 0.) * self.w_momentum
                vw.copy_(w - xi * (m + g + self.w_weight_decay*w))


    def step(self, trn_X, trn_y, val_X, val_y, xi, w_optim, a_optim):
        """ Compute unrolled loss and backward its gradients
        Args:
            xi: learning rate for virtual gradient step (same as net lr)
            w_optim: weights optimizer - for virtual step
        """
        a_optim.zero_grad()
        
        # sample k
        NASModule.param_module_call('sample_ops', n_samples=self.n_samples)
        
        if not self.unrolled:
            loss = self.net.loss(val_X, val_y)
            self.net.alpha_backward(loss)
        else:        
            # do virtual step (calc w`)
            self.virtual_step(trn_X, trn_y, xi, w_optim)

            # calc unrolled loss
            loss = self.v_net.loss(val_X, val_y) # L_val(w`)

            # compute gradient
            v_weights = tuple(self.v_net.weights())
            dw = torch.autograd.grad(loss, v_weights)
            dalpha = self.net.alpha_grad(loss)

            hessian = self.compute_hessian(dw, trn_X, trn_y)

            # update final gradient = dalpha - xi*hessian
            with torch.no_grad():
                for alpha, da, h in zip(self.net.alphas(), dalpha, hessian):
                    alpha.grad = da - xi*h
        
        # renormalization
        if self.renorm:
            prev_pw = []
            for p, m in NASModule.param_modules():
                s_op = m.get_state('s_op')
                pdt = p.detach()
                pp = pdt.index_select(-1,s_op)
                k = torch.sum(torch.exp(pdt)) / torch.sum(torch.exp(pp)) - 1
                # print(k)
                prev_pw.append(k)

        a_optim.step()

        # renormalization
        if self.renorm:
            for kprev, (p, m) in zip(prev_pw, NASModule.param_modules()):
                s_op = m.get_state('s_op')
                pdt = p.detach()
                pp = pdt.index_select(-1,s_op)
                k = torch.sum(torch.exp(pdt)) / torch.sum(torch.exp(pp)) - 1
                # print(k-kprev)
                for i in s_op:
                    p[i] += (torch.log(k) - torch.log(kprev))

        NASModule.module_call('reset_ops')

    def compute_hessian(self, dw, trn_X, trn_y):
        """
        dw = dw` { L_val(w`, alpha) }
        w+ = w + eps * dw
        w- = w - eps * dw
        hessian = (dalpha { L_trn(w+, alpha) } - dalpha { L_trn(w-, alpha) }) / (2*eps)
        eps = 0.01 / ||dw||
        """
        norm = torch.cat([w.view(-1) for w in dw if not w is None]).norm()
        eps = 0.01 / norm

        # print('weight start')
        # w+ = w + eps*dw`
        with torch.no_grad():
            for p, d in zip(self.net.weights(check_grad=True), dw):
                if not p is None and not d is None:
                    p += eps * d.to(p.device)
        loss = self.net.loss(trn_X, trn_y)
        dalpha_pos = torch.autograd.grad(loss, self.net.alphas(), allow_unused=True) # dalpha { L_trn(w+) }

        # w- = w - eps*dw`
        with torch.no_grad():
            for p, d in zip(self.net.weights(check_grad=True), dw):
                if not p is None and not d is None:
                    p -= 2. * eps * d.to(p.device)
        loss = self.net.loss(trn_X, trn_y)
        dalpha_neg = torch.autograd.grad(loss, self.net.alphas(), allow_unused=True) # dalpha { L_trn(w-) }

        # recover w
        with torch.no_grad():
            for p, d in zip(self.net.weights(check_grad=True), dw):
                if not p is None and not d is None:
                    p += eps * d.to(p.device)

        hessian = [((p-n) / 2.*eps if not p is None else 0) for p, n in zip(dalpha_pos, dalpha_neg)]
        return hessian