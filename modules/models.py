import math
import torch
import torch.nn as nn
import torch.nn.functional as F

def attention_calc(q, k, v, mode='dot', mask=None, scale=False):
    '''
    only dot attention is currenty supported
    '''
    attn_logits = torch.matmul(q, k.transpose(-2, -1))
    if scale:
        d_k = q.size()[-1]
        attn_logits = attn_logits / math.sqrt(d_k)
    if mask is not None:
        attn_logits = attn_logits.masked_fill(mask == 0, -9e15)
    attention = F.softmax(attn_logits, dim=-1)
    values = torch.matmul(attention, v)
    return values, attention

class _Dense(nn.Module):
    def __init__(self, in_features, out_features, dropout):
        super(_Dense, self).__init__()
        self.linear = nn.Sequential(
            nn.Linear(in_features, out_features),
            nn.Dropout(dropout),
            nn.ReLU(inplace=True),
        )
    def forward(self, x):
        return self.linear(x)

class _Cnn1(nn.Module):
    def __init__(self, in_channels, out_channels, kernel_size, dropout):
        super(_Cnn1, self).__init__()

        left, right = kernel_size//2, kernel_size//2
        if kernel_size%2==0 :
            right -= 1
        padding = (left, right, 0, 0)

        self.conv = nn.Sequential(
            nn.ZeroPad2d(padding),
            nn.Conv1d(in_channels, out_channels, kernel_size),
            nn.Dropout(dropout),
            nn.ReLU(inplace=True),
        )
    def forward(self, x):
        return self.conv(x)


class S2P(nn.Module):

    def __init__(self, window_size, dropout=0, lr=None):
        super(S2P, self).__init__()
        self.MODEL_NAME = 'Sequence2Point model'
        self.drop = dropout
        self.lr = lr

        self.dense_input = 50*window_size #50 is the out_features of last CNN1

        self.conv = nn.Sequential(
            _Cnn1(1, 30, kernel_size=10, dropout=self.drop),
            _Cnn1(30, 40, kernel_size=8, dropout=self.drop),
            _Cnn1(40, 50, kernel_size=6, dropout=self.drop),
            _Cnn1(50, 50, kernel_size=5, dropout=self.drop),
            _Cnn1(50, 50, kernel_size=5, dropout=self.drop),
            nn.Flatten()
        )
        self.dense = _Dense(self.dense_input, 1024, self.drop)
        self.output = nn.Linear(1024, 1)

    def forward(self, x):
        # x must be in shape [batch_size, 1, window_size]
        # eg: [1024, 1, 50]
        x = x
        x = x.unsqueeze(1)

        x = self.conv(x)
        x = self.dense(x)
        out = self.output(x)
        return out

class WGRU(nn.Module):

    def __init__(self, dropout=0,lr=None):
        super(WGRU, self).__init__()

        self.drop = dropout
        self.lr = lr

        self.conv1 = _Cnn1(1, 16, kernel_size=4,dropout=self.drop)

        self.b1 = nn.GRU(16, 64, batch_first=True,
                           bidirectional=True,
                           dropout=self.drop)
        self.b2 = nn.GRU(128, 256, batch_first=True,
                           bidirectional=True,
                           dropout=self.drop)

        self.dense1 = _Dense(512, 128, self.drop)
        self.dense2 = _Dense(128, 64, self.drop)
        self.output = nn.Linear(64, 1)

    def forward(self, x):
        # x must be in shape [batch_size, 1, window_size]
        # eg: [1024, 1, 50]
        x = x
        x = x.unsqueeze(1)
        x = self.conv1(x)
        # x (aka output of conv1) shape is [batch_size, out_channels=16, window_size-kernel+1]
        # x must be in shape [batch_size, seq_len, input_size=output_size of prev layer]
        # so we have to change the order of the dimensions
        x = x.permute(0, 2, 1)
        x = self.b1(x)[0]
        x = self.b2(x)[0]
        # we took only the first part of the tuple: output, h = gru(x)

        # Next we have to take only the last hidden state of the last b2gru
        # equivalent of return_sequences=False
        x = x[:, -1, :]
        x = self.dense1(x)
        x = self.dense2(x)
        out = self.output(x)
        return out


class SAED(nn.Module):

    def __init__(self, mode='dot', hidden_dim=16, num_heads=1, dropout=0,lr=None):
        super(SAED, self).__init__()

        '''
        mode(str): 'dot' or 'add'
            default is 'dot' (additive attention not supported yet)
        ***in order for the mhattention to work, embed_dim should be dividable
        to num_heads (embed_dim is the hidden dimension inside mhattention
        '''
        if num_heads > hidden_dim:
            num_heads = 1
            print('WARNING num_heads > embed_dim so it is set equal to 1')
        else:
            while hidden_dim%num_heads:
                if num_heads > 1:
                    num_heads -= 1
                else:
                    num_heads += 1

        self.drop = dropout
        self.lr = lr
        self.mode = 'dot'

        self.conv = _Cnn1(1, hidden_dim,
                           kernel_size=4,
                           dropout=self.drop)
        self.multihead_attn = nn.MultiheadAttention(embed_dim=hidden_dim,
                                                    num_heads=num_heads,
                                                    dropout=self.drop)
        self.bgru = nn.GRU(hidden_dim, 64,
                         batch_first=True,
                         bidirectional=True,
                         dropout=self.drop)
        self.dense = _Dense(128, 64, self.drop)
        self.output = nn.Linear(64, 1)

    def forward(self, x):
        # x must be in shape [batch_size, 1, window_size]
        # eg: [1024, 1, 50]
        x = x
        x = x.unsqueeze(1)
        x = self.conv(x)
        # x (aka output of conv1) shape is [batch_size, out_channels=16, window_size-kernel+1]
        # x must be in shape [batch_size, seq_len, input_size=output_size of prev layer]
        # so we have to change the order of the dimensions
        x = x.permute(0, 2, 1)
        # attn_output, attn_output_weights = multihead_attn(query, key, value)
        # x, _ = self.multihead_attn(query=x, key=x, value=x)
        x, _ = attention_calc(q=x, k=x, v=x, mode=self.mode)
        x = self.bgru(x)[0]
        # we took only the first part of the tuple: output, h = gru(x)

        # Next we have to take only the last hidden state of the last b1gru
        # equivalent of return_sequences=False
        x = x[:, -1, :]
        x = self.dense(x)
        out = self.output(x)
        return out

class SimpleGru(nn.Module):

    def __init__(self, hidden_dim=16, dropout=0,lr=None):
        super(SimpleGru, self).__init__()

        '''
        mode(str): 'dot' or 'add'
            default is 'dot' (additive attention not supported yet)
        ***in order for the mhattention to work, embed_dim should be dividable
        to num_heads (embed_dim is the hidden dimension inside mhattention
        '''

        self.drop = dropout
        self.lr = lr

        self.conv = _Cnn1(1, hidden_dim,
                           kernel_size=4,
                           dropout=self.drop)

        self.bgru = nn.GRU(hidden_dim, 64,
                         batch_first=True,
                         bidirectional=True,
                         dropout=self.drop)
        self.dense = _Dense(128, 64, self.drop)
        self.output = nn.Linear(64, 1)

    def forward(self, x):
        # x must be in shape [batch_size, 1, window_size]
        # eg: [1024, 1, 50]
        x = x
        x = x.unsqueeze(1)
        x = self.conv(x)
        # x (aka output of conv1) shape is [batch_size, out_channels=16, window_size-kernel+1]
        # x must be in shape [batch_size, seq_len, input_size=output_size of prev layer]
        # so we have to change the order of the dimensions
        x = x.permute(0, 2, 1)
        x = self.bgru(x)[0]
        # we took only the first part of the tuple: output, h = gru(x)

        # Next we have to take only the last hidden state of the last b1gru
        # equivalent of return_sequences=False
        x = x[:, -1, :]
        x = self.dense(x)
        out = self.output(x)
        return out