import torch
import numpy as np

from .blurring import dct_2d, idct_2d

def expand_t_like_x(t, x):
    """Function to reshape time t to broadcastable dimension of x
    Args:
        t: [batch_dim,], time vector
        x: [batch_dim, ...], data point
    """

    dims = [1] * (len(x.size()) - 1)
    t = t.view(t.size(0), *dims)
    return t

# ---------------------------------------------------------------------------------------------------------------------
# Coupling plans
# ---------------------------------------------------------------------------------------------------------------------

class ICPlan:
    """Linear Coupling Plan"""

    def __init__(self, sigma=0.0, diffusion_form="none", use_blurring=False, blur_simga_max=3, blur_upscale=4):
        self.sigma = sigma
        self.diffusion_form = diffusion_form
        self.use_blurring = use_blurring
        self.blur_sigma_max = blur_simga_max
        self.blur_upscale = blur_upscale
    
    def compute_alpha_t(self, t):
        """
        Compute the data coefficient along the path
        CondOT path construction, alpha_t = t, alpha_t_dot = 1
        """
        return t, 1 # return alpha_t and the slope alpha_t_dot

    def compute_beta_t (self, t):
        """
        Compute the noise coefficient along the path
        CondOT path construction, beta_t = 1 - t, beta_t_dot = -1
        """
        return 1-t, -1 # return beta_t and beta_t_dot, the slope
    
    # function to compute the ratio (d_alpha / dt)/alpha_t (alpha_t_dot = time derivative of alpha_t, slope of alpha_t)
    def compute_d_alpha_alpha_ratio_t (self, t):
        """Compute the ratio between d_alpha_t and alpha_t; simply 1/t for condOT path construction"""
        return 1 / t

    def compute_drift(self, x, t):
        # TODO: I dont like this implementation, it uses wrong terminologies and conventions 
        # further negates the coefficient of x in the score parametrization of conditional vectorfield while returning the two coefficients
        """
        We always output VectorField ut_theta(x|z) according to score parametrization; (not to mention minimizer theta for this conditional object also minimizes the corresponding marginal object (refer lecture notes))
        For Gaussian probability paths, conditional score at time t, is linear combination of dirac(z) and x0 ~ N(0, Id)
        hence, conditional vector field (drift) can be represented as a function of score
        but only in case of Gaussian Probability Paths
        """
        t = expand_t_like_x(t, x)
        # alpha_t_dot / alpha_t = 1/t for condOT path
        alpha_ratio = self.compute_d_alpha_alpha_ratio_t(t)
        # beta_t = 1 - t, beta_t_dot = -1
        beta_t, beta_t_dot = self.compute_beta_t(t)
        
        # idk why we are pretending that term of x in the score parametrization is called drift
        # idk why we are pretending that coeff of conditional score is called diffusion
        # idk why the heck we are returning negative of coefficient of x in the score parametrization, in formula its clearly addition
        drift = alpha_ratio * x
        score_coefficient = alpha_ratio * (beta_t**2) - beta_t * beta_t_dot
        # why stray away from mathematical convention and negate for reason
        return drift, score_coefficient
    
    # not to be confused with what they call diffusion above lol
    # looks like diffusion coefficient sigma_t
    # twisted repository
    def compute_diffusion(self, x, t, form="constant", norm=1.0):
        """Compute the diffusion term of the SDE
        Args:
            x: (batch_dim, ...), data point
            t: (batch_dim, ...), time vector
            form: str, form of the diffusion term
            norm: float, norm of the diffusion term
        """

        t = expand_t_like_x(t, x)
        choices = {
            "none": torch.zeros((1,), device=t.device),
            "constant": torch.full((1,), norm, device=t.device),
            "SBDM": norm * 2.0 * self.compute_drift(x, t)[1],
            "sigma": norm * self.compute_beta_t(t)[0], # TODO:this repository has so many issues following Flow Matching literature lol
            "linear": norm * (1-t), # why are we pretending that "diffusion" means beta_t and the coefficient of score in the equation of conditional vector field as linear combination of score at the same time lol
            "decreasing": 0.25 * (norm * torch.cos(np.pi * t) + 1) ** 2,
            "increasing-decreasing": norm * torch.sin(np.pi * t) ** 2,
            "log": norm * torch.log(t - t**2 + 1),
        }

        try:
            diffusion = choices[form]
        except KeyError:
            raise NotImplementedError(f"Diffusion form {form}, rather the beta_t schedule, is not implemented")

        return diffusion
    
    def compute_d_diffusion(self, x, t, form="constant", norm=1.0):
        """Compute the derivative of diffusion term of the SDE
        Args:
            x: (B, ...), data point x_t on the trajectory
            t: (B), time
            form: str, form of the diffusion term
            norm: float, norm of the diffusion term
        """
        t = expand_t_like_x(t, x)
        choices = {
            "none": torch.zeros((1,), device=t.device),
            "constant": torch.zeros((1,), device=t.device),
            #"SBDM": norm * 2.0 * self.compute_drift(x, t)[1],
            #"sigma": norm * self.compute_beta_t(t)[0],
            "linear": torch.full((1,), -norm), # negative slope implies noise injection tends to zero towards t=1
            "decreasing": -0.5
            * np.pi
            * norm
            * torch.sin(np.pi * t)
            * (norm * torch.cos(np.pi * t) + 1), # derivative of 0.25 * (norm * cos(pi * t) + 1) ** 2
            "increasing-decreasing": 2
            * norm
            * np.pi
            * torch.sin(np.pi * t)
            * torch. cos(np.pi * t), # derivative of norm * sin (pi * t)^ 2
            "log" : norm * (1 - 2 * t) / (t - t**2 + 1)
        }

        try:
            diffusion = choices[form]
        except KeyError:
            raise NotImplementedError(f"Diffusion form {form} is not implemented")
        return diffusion
    
    
    def get_score_from_vector_field (self, vector_field, x, t):
        """Transform vector field to score function
        Args:
            vector_field : (B, ...) shaped tensor
            x: (B, ...); x_t data point on the trajectory at time t
            t: (B) time 
        """

        t = expand_t_like_x(t, x) # (B, ...) matches shape of x_t
        alpha_t, alpha_t_dot = self.compute_alpha_t(t)
        beta_t, beta_t_dot = self.compute_beta_t(t)
        mean = x # (B, ...)
        reverse_alpha_ratio = alpha_t / alpha_t_dot
        var = beta_t**2 - reverse_alpha_ratio * beta_t_dot * beta_t
        score = (reverse_alpha_ratio * vector_field - mean) / var
        return score
    
    # Just substitute Score Function with -eps/beta_t as derived in denoising score matching objective (conditional score matching objective)
    def get_noise_from_vector_field(self, vector_field, x, t):
        """Transform vector_field u_t_theta(x_t) model to denoiser
        Args:
            vector_field: (B, ...), vectorfield_model
            x: (B, ...), x_t data point on the trajectory at time t
            t: (B) time
        """

        t = expand_t_like_x(t, x) # (B, 1, 1, 1) in case x is (B, C, H, W)
        alpha_t, alpha_t_dot = self.compute_alpha_t(t)
        beta_t, beta_t_dot = self.compute_beta_t(t)

        mean = x
        reverse_alpha_ratio = alpha_t / alpha_t_dot
        var = reverse_alpha_ratio * beta_t_dot - beta_t
        noise = (reverse_alpha_ratio* vector_field - mean) / var
        return noise
    
    def get_vector_field_from_score (self, score, x, t):
        """Transform score model to VectorField model
        Args:
            score: (B, ...), score model output
            x: (B, ...), x_t data point the trajectory at time t
            t: (B,) time
        """

        t = expand_t_like_x(t, x)
        x_term, score_coefficient = self.compute_drift(x, t)
        vectorField = score_coefficient * score + x_term
        return vectorField
    
    def compute_mu_t (self, t, x0, x1):
        """Compute the mean of along the conditional probability path p_t(.|z)"""
    