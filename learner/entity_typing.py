import torch
import torch.nn as nn
from torch.utils.data import DataLoader

from allennlp.modules.seq2vec_encoders.boe_encoder import BagOfEmbeddingsEncoder
from allennlp.nn.util import add_positional_features,weighted_sum

from utils.evaluate import strict,loose_macro,loose_micro

from utils.util import one_hot_to_labels
from utils.module import LSTMEncoder
from dataloader.entity_typing_dataloader import EntityTypingDataset



device = torch.device('cuda' if torch.cuda.is_available() else 'cpu')


class SelfAttentiveEncoder(nn.Module):

    def __init__(self, args):
        "docstring"
        super(SelfAttentiveEncoder, self).__init__()

        self.lstm = LSTMEncoder(
            input_size=args.word_dim,
            hidden_size=args.hidden_dim,
            num_layers=1,
            dropout=0.0,
            batch_first=True,
            bidirectional=True
        )

        self.proj = nn.Sequential(
            nn.Linear(2*args.hidden_dim,args.attention_dim),
            nn.Tanh(),
            nn.Linear(args.attention_dim,1),
        )

    def forward(self,c_l,c_l_lengths,c_r,c_r_lengths):

        c_l_repre,_ = self.lstm(c_l,c_l_lengths,need_sort=True)
        c_r_repre,_ = self.lstm(c_r,c_r_lengths,need_sort=True)

        matrix = torch.cat([c_l_repre,c_r_repre],dim=1)

        l_proj = self.proj(c_l_repre)  # bsize * (length_l) * 1
        r_proj = self.proj(c_r_repre)

        weights = torch.cat([l_proj,r_proj],dim=1).squeeze()
        weights = torch.nn.functional.softmax(weights,dim=1)

        return weighted_sum(matrix,weights)


class Model(nn.Module):

    def __init__(self, args):
        "docstring"
        super(Model, self).__init__()

        if args.word_pretrained is None:
            self.word_embedding = nn.Embedding(args.n_words,args.word_dim,args.padding_idx)
        else:
            self.word_embedding = nn.Embedding.from_pretrained(args.word_pretrained,freeze=args.freeze)

        self.label_embedding = nn.Embedding(args.n_labels,args.label_dim)

        if not args.share_vocab:
            if args.label_word_pretrained is None:
                self.label_word_embedding = nn.Embedding(args.n_labelwords,args.label_word_dim,args.padding_idx)
            else:
                self.label_word_embedding = nn.Embedding.from_pretrained(args.label_word_pretrained,freeze=args.freeze)

        self.mention_encoder = BagOfEmbeddingsEncoder(args)

        self.context_encoder = SelfAttentiveEncoder(args)

        self.label_encoder = BagOfEmbeddingsEncoder(args)

        cls_input_dim = args.word_dim + 2*args.hidden_dim

        self.repre_proj = nn.Sequential(
            nn.Linear(cls_input_dim,args.label_word_dim,bias=False),
            nn.ReLU()
        )
        self.hidden_dim = args.hidden_dim
        self.label_word_dim = args.label_word_dim

        self.n_labels = args.n_labels

        self.share_voacb = args.share_vocab

        self.use_position_embedding = args.use_position_embedding
        self.padding_idx = args.padding_idx

        self.loss_fn = nn.BCEWithLogitsLoss()

        self.optimizer = torch.optim.Adam(self.parameters(),args.lr)

        self.dropout = nn.Dropout(p=0.5)

    def forward(self,mention,left_context,right_context,candidate_labels,feature=None):

        bsize= mention.size()[0]
        mention_mask = (mention != self.padding_idx).float()

        mention_repre = self.word_embedding(mention)
        mention_repre = self.mention_encoder.forward(mention_repre,mask=mention_mask)

        mention_repre = self.dropout(mention_repre)

        left_context_lengths = (left_context != self.padding_idx).sum(dim=1).long().to(device)
        right_context_lengths = (right_context != self.padding_idx).sum(dim=1).long().to(device)

        left_context = self.word_embedding(left_context)
        right_context = self.word_embedding(right_context)

        if self.use_position_embedding:
            left_context = add_positional_features(left_context)
            right_context = add_positional_features(right_context)

        context_repre = self.context_encoder(left_context,left_context_lengths,right_context,right_context_lengths)

        mention_repre = self.repre_proj(torch.cat([mention_repre,context_repre],dim=-1)) # bsize * hidden_dim

        label_mask = (candidate_labels != self.padding_idx).float()
        n_classes = candidate_labels.size()[1]
        label_len = candidate_labels.size()[2]

        if self.share_voacb:
            label_repre = self.word_embedding(candidate_labels)
        else:
            label_repre = self.label_word_embedding(candidate_labels) # bsize * N_CLASSES * hidden_dim
        label_repre = self.label_encoder.forward(label_repre.view(bsize*n_classes,label_len,-1),label_mask.view(bsize*n_classes,label_len)).view(bsize,n_classes,-1)

        score = torch.bmm(label_repre,mention_repre.view(bsize,self.label_word_dim,1)) # bsize * N_CLASSES * 1

        return score.squeeze()

    def train_epoch(self,train_iter):

        self.train()
        total_batch = len(train_iter)
        loss = 0.0
        cur_batch = 1
        for batch in train_iter:
            mention = torch.from_numpy(batch['mention']).to(device)
            left_context = torch.from_numpy(batch['left_context']).to(device)
            right_context = torch.from_numpy(batch['right_context']).to(device)
            candidate_labels = torch.from_numpy(batch['full_labels']).to(device)
            labels = torch.from_numpy(batch['labels']).to(device)

            output = self.forward(mention,left_context,right_context,candidate_labels)# bsize * N_CLASSES
            batch_loss = self.loss_fn(output,labels.float())
            self.optimizer.zero_grad()
            batch_loss.backward()
            self.optimizer.step()
            cur_batch += 1

            loss += batch_loss
            print('\r Batch {}/{}, Training Loss:{}'.format(cur_batch,total_batch,loss/cur_batch),end='')

    def evaluate(self,dev_iter):

        self.eval()
        target_labels = []
        pred_labels = []
        for batch in dev_iter:
            mention = torch.tensor(batch['mention']).to(device)
            left_context = torch.tensor(batch['left_context']).to(device)
            right_context = torch.tensor(batch['right_context']).to(device)
            labels = torch.tensor(batch['labels']).to(device)
            candidate_labels = torch.tensor(batch['full_labels']).to(device)

            output = self.forward(mention,left_context,right_context,candidate_labels)

            # ensure at least one label
            one_label = output.argmax(dim=1)

            pred = (torch.sigmoid(output) > 0.5)
            target_labels.extend(one_hot_to_labels(labels))
            pred_labels.extend(one_hot_to_labels(pred,one_label))

        true_and_prediction = list(zip(target_labels,pred_labels))
        return strict(true_and_prediction)[2],loose_macro(true_and_prediction)[2],loose_micro(true_and_prediction)[2]




class TestConfig:

    def __init__(args):
        args.word_dim = 30
        args.hidden_dim = 30
        args.attention_dim = 10
        args.label_dim = 10
        args.label_word_dim = 30
        args.batch_size = 10
        args.share_vocab =True
        args.epoch = 20
        args.use_position_embedding = False
        args.data_dir = '../../data/FIGER-gold'
        # args.pretrained_path = '/home/user_data/lijh/data/english_embeddings/glove.840B.300d.txt'
        args.word_pretrained_path = None
        args.label_word_pretrained_path = None
        args.word_pretrained = None
        args.label_word_pretrained = None

        args.padding_idx = 0
        args.lr = 1e-3

        args.vocab_pth = './data/FIGER/share_vocab/vocab.pkl'
        args.train_dataset_pth = './data/FIGER/share_vocab/train.pkl'
        args.dev_dataset_pth = './data/FIGER/share_vocab/dev.pkl'
        args.test_dataset_pth = './data/FIGER/share_vocab/test.pkl'

class DefaultConfig:

    def __init__(args):
        args.word_dim = 300
        args.hidden_dim = 100
        args.attention_dim = 50
        args.label_dim = 300
        args.label_word_dim = 300
        args.batch_size = 1024
        args.epoch = 10
        args.use_position_embedding = False
        args.share_vocab = True
        args.data_dir = '/home/user_data55/lijh/data/FIGER'

        args.word_pretrained_path = './data/FIGER/share_vocab/word_pretrained.pth'
        args.label_word_pretrained_path = './data/FIGER/share_vocab/label_pretrained.pth'
        args.lr = 1e-3
        args.padding_idx = 0

        args.vocab_pth = './data/FIGER/share_vocab/vocab.pkl'
        args.train_dataset_pth = './data/FIGER/share_vocab/train.pkl'
        args.dev_dataset_pth = './data/FIGER/share_vocab/dev.pkl'
        args.test_dataset_pth = './data/FIGER/share_vocab/test.pkl'

        args.save_pth = 'zero_shot.pth'

        args.freeze = True


if __name__ == '__main__':
    import sys
    if sys.argv[1] == '--test':
        args = TestConfig()
        args.glove_pth = None
        args.mode = 'supervised'
        main(args)
    elif sys.argv[1] == '--supervised':
        args = DefaultConfig()
        args.mode = 'supervised'
        main(args)
    elif sys.argv[1] == '--zero-shot':
        args = DefaultConfig()
        args.mode = 'zero-shot'
        main(args)
    elif sys.argv[1] == '--generate':
        args = DefaultConfig()
        args.glove_pth = '/home/user_data/lijh/data/english_embeddings/glove.840B.300d.txt'
        EntityTypingDataset.generate_dataset(args)
        EntityTypingDataset.generate_embedding(args,device)