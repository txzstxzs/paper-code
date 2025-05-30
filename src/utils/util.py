import os
import numpy as np
import torch
import random


def flatten(v):
    """
    Flatten a list of lists/tuples
    """

    return [x for y in v for x in y]


def find_max_epoch(path):
    """
    Find maximum epoch/iteration in path, formatted ${n_iter}.pkl
    E.g. 100000.pkl

    Parameters:
    path (str): checkpoint path
    
    Returns:
    maximum iteration, -1 if there is no (valid) checkpoint
    """

    files = os.listdir(path)
    epoch = -1
    for f in files:
        if len(f) <= 4:
            continue
        if f[-4:] == '.pkl':
            try:
                epoch = max(epoch, int(f[:-4]))
            except:
                continue
    return epoch


def print_size(net):
    """
    Print the number of parameters of a network
    """

    if net is not None and isinstance(net, torch.nn.Module):
        module_parameters = filter(lambda p: p.requires_grad, net.parameters())
        params = sum([np.prod(p.size()) for p in module_parameters])
        print("{} Parameters: {:.6f}M".format(
            net.__class__.__name__, params / 1e6), flush=True)


# Utilities for diffusion models

def std_normal(size):
    """
    Generate the standard Gaussian variable of a certain size
    """

    return torch.normal(0, 1, size=size).cuda()


def calc_diffusion_step_embedding(diffusion_steps, diffusion_step_embed_dim_in):
    """
    Embed a diffusion step $t$ into a higher dimensional space
    E.g. the embedding vector in the 128-dimensional space is
    [sin(t * 10^(0*4/63)), ... , sin(t * 10^(63*4/63)), cos(t * 10^(0*4/63)), ... , cos(t * 10^(63*4/63))]

    Parameters:
    diffusion_steps (torch.long tensor, shape=(batchsize, 1)):     
                                diffusion steps for batch data
    diffusion_step_embed_dim_in (int, default=128):  
                                dimensionality of the embedding space for discrete diffusion steps
    
    Returns:
    the embedding vectors (torch.tensor, shape=(batchsize, diffusion_step_embed_dim_in)):
    """

    assert diffusion_step_embed_dim_in % 2 == 0

    half_dim = diffusion_step_embed_dim_in // 2
    _embed = np.log(10000) / (half_dim - 1)
    _embed = torch.exp(torch.arange(half_dim) * -_embed).cuda()
    _embed = diffusion_steps * _embed
    diffusion_step_embed = torch.cat((torch.sin(_embed),
                                      torch.cos(_embed)), 1)

    return diffusion_step_embed

'计算具体的beta alpha等值'
def calc_diffusion_hyperparams(T, beta_0, beta_T):
    """
    Compute diffusion process hyperparameters

    Parameters:
    T (int):                    number of diffusion steps
    beta_0 and beta_T (float):  beta schedule start/end value, 
                                where any beta_t in the middle is linearly interpolated
    
    Returns:
    a dictionary of diffusion hyperparameters including:
        T (int), Beta/Alpha/Alpha_bar/Sigma (torch.tensor on cpu, shape=(T, ))
        These cpu tensors are changed to cuda tensors on each individual gpu
    """

    Beta = torch.linspace(beta_0, beta_T, T)  # Linear schedule
    Alpha = 1 - Beta
    Alpha_bar = Alpha + 0
    Beta_tilde = Beta + 0
    for t in range(1, T):
        Alpha_bar[t] *= Alpha_bar[t - 1]  # \bar{\alpha}_t = \prod_{s=1}^t \alpha_s
        Beta_tilde[t] *= (1 - Alpha_bar[t - 1]) / (
                1 - Alpha_bar[t])  # \tilde{\beta}_t = \beta_t * (1-\bar{\alpha}_{t-1})
        # / (1-\bar{\alpha}_t)
    Sigma = torch.sqrt(Beta_tilde)  # \sigma_t^2  = \tilde{\beta}_t

    _dh = {}
    _dh["T"], _dh["Beta"], _dh["Alpha"], _dh["Alpha_bar"], _dh["Sigma"] = T, Beta, Alpha, Alpha_bar, Sigma
    diffusion_hyperparams = _dh
    return diffusion_hyperparams



'基本就是ddpm里的逆向采样 缺失处为随机噪声 逐步对缺失处减噪  多了监督条件 已有值和掩码拼接作为监督'
def sampling(net, size, diffusion_hyperparams, cond, mask, only_generate_missing=0, guidance_weight=0):
    """
    Perform the complete sampling step according to p(x_0|x_T) = \prod_{t=1}^T p_{\theta}(x_{t-1}|x_t)

    Parameters:
    net (torch network):            the wavenet model
    size (tuple):                   size of tensor to be generated, 
                                    usually is (number of audios to generate, channels=1, length of audio)
    diffusion_hyperparams (dict):   dictionary of diffusion hyperparameters returned by calc_diffusion_hyperparams
                                    note, the tensors need to be cuda tensors 
    
    Returns:
    the generated audio(s) in torch.tensor, shape=size
    """

    _dh = diffusion_hyperparams
    T, Alpha, Alpha_bar, Sigma = _dh["T"], _dh["Alpha"], _dh["Alpha_bar"], _dh["Sigma"]
    assert len(Alpha) == T
    assert len(Alpha_bar) == T
    assert len(Sigma) == T
    assert len(size) == 3

    print('begin sampling, total number of reverse steps = %s' % T)

    x = std_normal(size)      # xT  shape为 [采样数, 14 ,100]

    with torch.no_grad(): 
        
        for t in range(T - 1, -1, -1):                       # 开始减噪
            
            if only_generate_missing == 1:
                x = x * (1 - mask).float() + cond * mask.float()   # 开始时缺失处填噪声 cond是原数据 和训练时一样  看training_loss模块  
                                                   # 每一次迭代 缺失处替换为上一步的减噪结果
                                                   # 也就是逐步对缺失处减噪
                
            diffusion_steps = (t * torch.ones((size[0], 1))).cuda()  # use the corresponding reverse step
            
            epsilon_theta = net((x, cond, mask, diffusion_steps,))  # predict \epsilon according to \epsilon_\theta
            
            # 计算xt-1 update x_{t-1} to \mu_\theta(x_t)
            x = (x - (1 - Alpha[t]) / torch.sqrt(1 - Alpha_bar[t]) * epsilon_theta) / torch.sqrt(Alpha[t])
            
            if t > 0:
                x = x + Sigma[t] * std_normal(size)  # x0不加标准差  add the variance term to x_{t-1}

    return x



'这里基本上是一个ddpm的加噪和预测噪声的过程 多了监督条件 缺失掩码'
'only_generate_missing为加噪方式 1是局部加噪 0是全局加噪'
def training_loss(net, loss_fn, X, diffusion_hyperparams, only_generate_missing=1):
    """
    Compute the training loss of epsilon and epsilon_theta

    Parameters:
    net (torch network):            the wavenet model
    loss_fn (torch loss function):  the loss function, default is nn.MSELoss()
    X (torch.tensor):               training data, shape=(batchsize, 1, length of audio)
    diffusion_hyperparams (dict):   dictionary of diffusion hyperparameters returned by calc_diffusion_hyperparams
                                    note, the tensors need to be cuda tensors       
    
    Returns:
    training loss
    """

    _dh = diffusion_hyperparams
    T, Alpha_bar = _dh["T"], _dh["Alpha_bar"]

    audio = X[0]      # 原数据
    cond = X[1]       # 也是原数据 但会乘以掩码再和掩码拼接 作为监督  其实这两个原数据用一个就行
    mask = X[2]       # 01掩码   1保留 0缺失
    loss_mask = X[3]    # bool掩码 用于计算损失  true为缺失 false为保留

    B, C, L = audio.shape                           # B is batchsize, C=1, L is audio length    [50, 14, 100]
#     print('audio cond', audio.shape, cond.shape)           # [50, 14, 100]  [50, 14, 100]
    
    diffusion_steps = torch.randint(T, size=(B, 1, 1)).cuda()  #  随机扩散步加噪 randomly sample diffusion steps from 1~T 

    z = std_normal(audio.shape)
    
#     if only_generate_missing == 1:                     # mask的1为保留 0为缺失 
#         z = audio * mask.float() + z * (1 - mask).float()    # 只对缺失处加噪 保留处不变  采样时也是这样 缺失处为噪声 看sampling模块
        
                                             # x0到xt 前向加噪 只对缺失处加噪  
    transformed_X = torch.sqrt(Alpha_bar[diffusion_steps]) * audio + torch.sqrt(1 - Alpha_bar[diffusion_steps]) * z    
                                            
    
    epsilon_theta = net( (transformed_X, cond, mask, diffusion_steps.view(B, 1),) )  # 预测噪声 predict \epsilon according to \epsilon_\theta 

#     if only_generate_missing == 1:         # 计算缺失处的噪声损失 对应掩码为true 所以要用掩码标出计算损失的位置
#         return loss_fn(epsilon_theta[loss_mask], z[loss_mask])
#     elif only_generate_missing == 0:        # 全局加噪 所以不用加掩码
    return loss_fn(epsilon_theta, z)


'mcar缺失场景 和下面三个缺失方法的输入参数不一样 这里直接输入缺失率即可'
def get_mask_mcar(sample, mr): 
    mask = torch.ones(sample.shape).view(1,-1) 
    index = torch.tensor(range(mask.shape[1]))  
    perm = torch.randperm(len(index)) 
    index = perm[ 0: round( mr*len(index) ) ]
    mask[0][index]=0
    mask = mask.view(sample.shape)
    return mask



    
'下面为获取三种类型的掩码'
'都是按列缺失 k为每列缺失的个数'
def get_mask_rm(sample, k):   # 随机缺失  输入为一个样本方块 [100, 14]  k为20 是每一列数据要缺失的数据个数  每一列缺失的个数都一样 有随机性  缺失率为20/100=0.2 
                     # 这个其实还不算mcar的完全随机缺失 因为这里每一列都要缺失相同的个数
    """Get mask of random points (missing at random) across channels based on k,
    where k == number of data points. Mask of sample's shape where 0's to be imputed, and 1's to preserved
    as per ts imputers"""

    mask = torch.ones(sample.shape)          # [100, 14]  
    length_index = torch.tensor(range(mask.shape[0]))    # 0-100 序列程度 时间步长度  lenght of series indexes
    
    for channel in range(mask.shape[1]):             # 对于每一列  一列一列缺失
        perm = torch.randperm(len(length_index))      # 0-100打乱
        idx = perm[0:k]                       # 取前20个
        mask[:, channel][idx] = 0                # 对于每一列 随机20行为0  原有100行 所以缺失率为0.2

    return mask


def get_mask_mnr(sample, k):  # 缺失的时候是连续k个点一起缺失 不那么随机 
    """Get mask of random segments (non-missing at random) across channels based on k,
    where k == number of segments. Mask of sample's shape where 0's to be imputed, and 1's to preserved
    as per ts imputers"""

    mask = torch.ones(sample.shape)
    length_index = torch.tensor(range(mask.shape[0]))
    list_of_segments_index = torch.split(length_index, k)
    for channel in range(mask.shape[1]):
        s_nan = random.choice(list_of_segments_index)
        mask[:, channel][s_nan[0]:s_nan[-1] + 1] = 0

    return mask


def get_mask_bm(sample, k):  # 和上面的类似 也是连续缺失 而且每一列的缺失情况都一样 也就是块缺失
    """Get mask of same segments (black-out missing) across channels based on k,
    where k == number of segments. Mask of sample's shape where 0's to be imputed, and 1's to be preserved
    as per ts imputers"""

    mask = torch.ones(sample.shape)
    length_index = torch.tensor(range(mask.shape[0]))
    list_of_segments_index = torch.split(length_index, k)
    s_nan = random.choice(list_of_segments_index)
    for channel in range(mask.shape[1]):
        mask[:, channel][s_nan[0]:s_nan[-1] + 1] = 0

    return mask
