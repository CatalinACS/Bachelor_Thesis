import torch
import torch.nn as nn
from diff_aug import DiffAugment
from utils.quantum_layer import *


class MLP(nn.Module):
    def __init__(
        self, in_feat, quantum_circuit, hid_feat=None, out_feat=None, dropout=0.0
    ):
        super().__init__()
        if not hid_feat:
            hid_feat = in_feat
        if not out_feat:
            out_feat = in_feat
        self.in_feat = in_feat
        self.fc1 = nn.Linear(in_feat, hid_feat)
        self.act = nn.GELU()
        self.fc2 = nn.Linear(hid_feat, out_feat)
        self.droprateout = nn.Dropout(dropout)
        self.quantum_w_shape = (1,)
        self.quantum_circuit = quantum_circuit

    def forward(self, x):
        x = self.fc1(x)
        x = QuantumLayer(
            num_qubits=self.in_feat,
            w_shape=self.quantum_w_shape,
            circuit=self.quantum_circuit,
        )(x)
        x = self.act(x)
        x = self.fc2(x)
        return self.droprateout(x)


class Attention(nn.Module):
    def __init__(
        self, dim, quantum_circuit, heads=4, attention_dropout=0.0, proj_dropout=0.0
    ):
        super().__init__()
        self.heads = heads
        self.scale = 1.0 / dim**0.5

        self.qkv = nn.Linear(dim, dim * 3, bias=False)
        self.attention_dropout = nn.Dropout(attention_dropout)
        self.out = nn.Sequential(nn.Linear(dim, dim), nn.Dropout(proj_dropout))
        self.quantum_w_shape = (1,)
        self.quantum_circuit = quantum_circuit

    def forward(self, x):
        b, n, c = x.shape
        batch_size, seq_len, hidden_size = x.shape
        head_dim = hidden_size // self.heads
        q, k, v = [
            proj(x).reshape(batch_size, seq_len, self.heads, head_dim).swapaxes(1, 2)
            for proj, x in zip(
                [
                    QuantumLayer(
                        num_qubits=hidden_size,
                        w_shape=self.quantum_w_shape,
                        circuit=self.quantum_circuit,
                    ),
                    QuantumLayer(
                        num_qubits=hidden_size,
                        w_shape=self.quantum_w_shape,
                        circuit=self.quantum_circuit,
                    ),
                    QuantumLayer(
                        num_qubits=hidden_size,
                        w_shape=self.quantum_w_shape,
                        circuit=self.quantum_circuit,
                    ),
                ],
                [x, x, x],
            )
        ]

        dot = (q @ k.transpose(-2, -1)) * self.scale
        attn = dot.softmax(dim=-1)
        attn = self.attention_dropout(attn)

        x = (attn @ v).transpose(1, 2).reshape(b, n, c)
        x = QuantumLayer(
            num_qubits=hidden_size,
            w_shape=self.quantum_w_shape,
            circuit=self.quantum_circuit,
        )(x)
        return x


class ImgPatches(nn.Module):
    def __init__(self, input_channel=3, dim=768, patch_size=4):
        super().__init__()
        self.patch_embed = nn.Conv2d(
            input_channel, dim, kernel_size=patch_size, stride=patch_size
        )

    def forward(self, img):
        patches = self.patch_embed(img).flatten(2).transpose(1, 2)
        return patches


def UpSampling(x, H, W):
    B, N, C = x.size()
    assert N == H * W
    x = x.permute(0, 2, 1)
    x = x.view(-1, C, H, W)
    x = nn.PixelShuffle(2)(x)
    B, C, H, W = x.size()
    x = x.view(-1, C, H * W)
    x = x.permute(0, 2, 1)
    return x, H, W


class Encoder_Block(nn.Module):
    def __init__(self, dim, quantum_circuit, heads, mlp_ratio=4, drop_rate=0.0):
        super().__init__()
        self.ln1 = nn.LayerNorm(dim)
        self.attn = Attention(
            dim, quantum_circuit, heads, drop_rate, drop_rate
        )
        self.ln2 = nn.LayerNorm(dim)
        self.mlp = MLP(
            dim, quantum_circuit, dim * mlp_ratio, dropout=drop_rate
        )

    def forward(self, x):
        x1 = self.ln1(x)
        x = x + self.attn(x1)
        x2 = self.ln2(x)
        x = x + self.mlp(x2)
        return x


class RK4_ENH_Block(nn.Module):
    def __init__(self, dim, quantum_circuit, heads, mlp_ratio=4, drop_rate=0.0):
        super().__init__()
        
        self.ln1 = nn.LayerNorm(dim)
        self.attn = Attention(
            dim, quantum_circuit, heads, drop_rate, drop_rate
        )
        self.ln2 = nn.LayerNorm(dim)
        self.mlp = MLP(
            dim, quantum_circuit, dim * mlp_ratio, dropout=drop_rate
        )

    def forward(self, x):
        x_norm = self.ln1(x)
        att_output1 = self.attn(x_norm)
        y1 = self.mlp(x_norm)
        att_output2 = self.attn(x_norm + att_output1 + y1)
        y2 = self.mlp(x_norm + att_output1 + y1)
        att_output3 = self.attn(x_norm + att_output2 + y2)
        y3 = self.mlp(x_norm + att_output2 + y2)
        att_output4 = self.attn(x_norm + att_output3 + y3)
        y4 = self.mlp(x_norm + att_output3 + y3)

        return x_norm + att_output1 + y1 + att_output4 + y4


class TransformerEncoder(nn.Module):
    def __init__(self, quantum_circuit, depth, dim, heads, mlp_ratio=4, drop_rate=0.0):
        super().__init__()
        if depth == 1:
            self.Encoder_Blocks = nn.ModuleList(
                [
                    RK4_ENH_Block(
                        dim,
                        quantum_circuit,
                        heads,
                        mlp_ratio,
                        drop_rate,
                        
                    )
                ]
            )
        else:
            self.Encoder_Blocks = nn.ModuleList(
                [
                    Encoder_Block(
                        dim,
                        quantum_circuit,
                        heads,
                        mlp_ratio,
                        drop_rate,
                    )
                    for i in range(depth)
                ]
            )

    def forward(self, x):
        for Encoder_Block in self.Encoder_Blocks:
            x = Encoder_Block(x)
        return x


class Generator(nn.Module):
    """docstring for Generator"""

    def __init__(
        self,
        quantum_circuit,
        depth1=5,
        depth2=4,
        depth3=2,
        initial_size=8,
        dim=384,
        heads=4,
        mlp_ratio=4,
        drop_rate=0.0,
    ):  # ,device=device):
        super(Generator, self).__init__()

        # self.device = device
        self.quantum_circuit = quantum_circuit
        self.initial_size = initial_size
        self.dim = dim
        self.depth1 = depth1
        self.depth2 = depth2
        self.depth3 = depth3
        self.heads = heads
        self.mlp_ratio = mlp_ratio
        self.droprate_rate = drop_rate

        self.mlp = nn.Linear(1024, (self.initial_size**2) * self.dim)

        self.positional_embedding_1 = nn.Parameter(torch.zeros(1, (8**2), 384))
        self.positional_embedding_2 = nn.Parameter(
            torch.zeros(1, (8 * 2) ** 2, 384 // 4)
        )
        self.positional_embedding_3 = nn.Parameter(
            torch.zeros(1, (8 * 4) ** 2, 384 // 16)
        )

        self.TransformerEncoder_encoder1 = TransformerEncoder(
            quantum_circuit=self.quantum_circuit,
            depth=self.depth1,
            dim=self.dim,
            heads=self.heads,
            mlp_ratio=self.mlp_ratio,
            drop_rate=self.droprate_rate,
        )
        self.TransformerEncoder_encoder2 = TransformerEncoder(
            quantum_circuit=self.quantum_circuit,
            depth=self.depth2,
            dim=self.dim // 4,
            heads=self.heads,
            mlp_ratio=self.mlp_ratio,
            drop_rate=self.droprate_rate,
        )
        self.TransformerEncoder_encoder3 = TransformerEncoder(
            quantum_circuit=self.quantum_circuit,
            depth=self.depth3,
            dim=self.dim // 16,
            heads=self.heads,
            mlp_ratio=self.mlp_ratio,
            drop_rate=self.droprate_rate,
        )

        self.linear = nn.Sequential(nn.Conv2d(self.dim // 16, 3, 1, 1, 0))

    def forward(self, noise):

        x = self.mlp(noise).view(-1, self.initial_size**2, self.dim)

        x = x + self.positional_embedding_1
        H, W = self.initial_size, self.initial_size
        x = self.TransformerEncoder_encoder1(x)

        x, H, W = UpSampling(x, H, W)
        x = x + self.positional_embedding_2
        x = self.TransformerEncoder_encoder2(x)

        x, H, W = UpSampling(x, H, W)
        x = x + self.positional_embedding_3

        x = self.TransformerEncoder_encoder3(x)
        x = self.linear(x.permute(0, 2, 1).view(-1, self.dim // 16, H, W))

        return x


class Discriminator(nn.Module):
    def __init__(
        self,
        diff_aug,
        quantum_circuit,
        image_size=32,
        patch_size=4,
        input_channel=3,
        num_classes=1,
        dim=384,
        depth=7,
        heads=4,
        mlp_ratio=4,
        drop_rate=0.0,
    ):
        super().__init__()
        if image_size % patch_size != 0:
            raise ValueError("Image size must be divisible by patch size.")
        num_patches = (image_size // patch_size) ** 2
        self.quantum_circuit = quantum_circuit
        self.diff_aug = diff_aug
        self.patch_size = patch_size
        self.depth = depth
        # Image patches and embedding layer
        self.patches = ImgPatches(input_channel, dim, self.patch_size)

        # Embedding for patch position and class
        self.positional_embedding = nn.Parameter(torch.zeros(1, num_patches + 1, dim))
        self.class_embedding = nn.Parameter(torch.zeros(1, 1, dim))
        nn.init.trunc_normal_(self.positional_embedding, std=0.2)
        nn.init.trunc_normal_(self.class_embedding, std=0.2)

        self.droprate = nn.Dropout(p=drop_rate)
        self.TransfomerEncoder = TransformerEncoder(
            self.quantum_circuit,
            depth,
            dim,
            heads,
            mlp_ratio,
            drop_rate,
        )

        self.norm = nn.LayerNorm(dim)
        self.out = nn.Linear(dim, num_classes)
        self.apply(self._init_weights)

    def _init_weights(self, m):
        if isinstance(m, nn.Linear):
            nn.init.trunc_normal_(m.weight, std=0.02)
            if isinstance(m, nn.Linear) and m.bias is not None:
                nn.init.constant_(m.bias, 0)
        elif isinstance(m, nn.LayerNorm):
            nn.init.constant_(m.bias, 0)
            nn.init.constant_(m.weight, 1.0)

    def forward(self, x):
        x = DiffAugment(x, self.diff_aug)
        b = x.shape[0]
        cls_token = self.class_embedding.expand(b, -1, -1)

        x = self.patches(x)
        x = torch.cat((cls_token, x), dim=1)
        x += self.positional_embedding
        x = self.droprate(x)
        x = self.TransfomerEncoder(x)
        x = self.norm(x)
        x = self.out(x[:, 0])
        return x
