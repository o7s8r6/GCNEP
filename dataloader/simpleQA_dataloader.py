import torch
from torch.utils.data import Dataset
from collections import defaultdict
import linecache
import os
import dill
import numpy as np
import random

from utils.util import pad,load_pretrained
from utils.graph_util import build_graph_from_triplets
from dataloader.vocab import SimpleQAVocab

device = torch.device('cuda' if torch.cuda.is_available() else 'cpu')


class SimpleQADataset(Dataset):

    def __init__(self,filename,vocab,batch_size,kb_triplets,ns=0,train=True):

        self.vocab = vocab
        self.read_file(filename,vocab)
        self.filename = filename
        self.batch_size = batch_size
        self.ns = ns
        self.kb_triplets = kb_triplets

    def read_file(self,filename,vocab):

        self.label_dict = defaultdict(lambda: [])
        self.label_set = set()
        cnt = 0
        self.length = 0
        with open(filename,'r') as f:
            for line in f:
                question,relation_words_p,relation_p = line.rstrip().split('\t')
                self.label_dict[relation_p].append(cnt)
                self.label_set.add(relation_p)
                cnt += 1
                self.length += 1

    def process_line(self,line):
        question,relation_words_p,relation_p = line.rstrip().split('\t')
        example = dict()
        example['question'] = question.split()
        example['relation_words_p'] = relation_words_p.split()
        example['relation_p'] = relation_p

        question = [self.vocab.stoi.get(word,1) for word in example['question']]
        relation_p = self.vocab.rtoi[example['relation_p']]
        relation_words_p = self.vocab.relIdx2wordIdx[relation_p]

        return {
            'question': question,
            'relation_words': [relation_words_p],
            'relation': [relation_p],
        }

    def get_label_set(self):
        return self.label_set

    def __len__(self):
        return self.length

    def __getitem__(self,item):
        line = linecache.getline(self.filename,item + 1)
        instance = self.process_line(line)
        pos = instance['relation'][0]
        if self.ns > 0:
            ns = 0
            while ns < self.ns:
                idx = random.randint(0,len(self.vocab.rtoi) - 1)
                if idx in instance['relation']:
                    continue
                instance['relation'].append(idx)
                ns += 1
        else:
            neg = [i for i in range(len(self.vocab.rtoi))]
            neg.remove(pos)
            instance['relation'] = [pos] + neg

        sampled_kb_triplets = list(filter(lambda x:x[1] == pos,self.kb_triplets))
        instance['kb_triplets'] = sampled_kb_triplets
        return instance

    @staticmethod
    def build_vocab(filenames,args):
        vocab = SimpleQAVocab()
        vocab.stoi = {'<pad>':0,'<unk>':1}
        vocab.rtoi = {}
        relation_set = set()
        with open(args.relation_file,'rb') as f:
            import pickle
            rel_voc = pickle.load(f)
            for key,val in rel_voc.items():
                try:
                    int(key)
                    pass
                except ValueError:
                    vocab.renew_vocab([key],'rtoi')
        for filepath in filenames:
            with open(filepath,'r') as f:
                for line in f:
                    question,relation_words_p,relation_p = line.rstrip().split('\t')
                    relation_set.add(relation_p)
                    vocab.renew_vocab(question.split(),'stoi')
                    vocab.renew_vocab(relation_words_p.split(),'stoi')

        for rel,idx in vocab.rtoi.items():
            relation_words = rel.replace('.',' ').replace('_',' ').split()
            vocab.relIdx2wordIdx[idx] = [vocab.stoi.get(w,1) for w in relation_words]

        return vocab

    @staticmethod
    def generate_dataset(args):
        filepaths = ['train.tsv','dev.tsv','test.tsv']
        for i in range(len(filepaths)):
            filepaths[i] = os.path.join(args.data_dir,filepaths[i])

        vocab = SimpleQADataset.build_vocab(filepaths,args)
        torch.save(vocab,args.vocab_pth)

        print('Saved Vocab')

    @staticmethod
    def generate_embedding(args,device):
        vocab = torch.load(args.vocab_pth)
        args.word_pretrained = load_pretrained(args.glove_pth,vocab.stoi,dim=args.word_dim,device=device,pad_idx=args.padding_idx)
        torch.save(args.word_pretrained,args.word_pretrained_pth)
        # args.label_pretrained = load_pretrained(args.transE_pth,vocab.rtoi,dim=args.relation_dim,device=device)
        # torch.save(args.label_pretrained,args.label_pretrained_pth)

    @staticmethod
    def generate_graph(args,device):
        vocab = torch.load(args.vocab_pth)
        entity_freq = defaultdict(lambda : 0)
        vocab.etoi = {'<unk>':1}
        triplets = []
        with open(args.graph_file,'r') as f:
            cnt = 0
            for line in f:
                h,r,t = line.rstrip().split()
                entity_freq[h] += 1
                entity_freq[r] += 1
                cnt += 1
                if cnt % 1000 == 0:
                    print('\r{}'.format(cnt),end='')

        print()
        for e in entity_freq:
            if entity_freq[e] > 0:
                vocab.renew_vocab([e],'etoi')
        print('Total Entities : {}'.format(len(vocab.etoi)))
        print('Total Relations: {}'.format(len(vocab.rtoi)))

        rcount = defaultdict(lambda : 0)

        with open(args.graph_file,'r') as f:
            cnt = 0
            for line in f:
                h,r,t = line.rstrip().split()
                if h not in vocab.etoi or t not in vocab.etoi or rcount[r] > 300:
                    continue
                triplets.append((h,r,t))
                rcount[r] += 1
                cnt += 1
                if cnt % 1000 == 0:
                    print('\r{}'.format(cnt),end='')
        vocab.etoi = {'<unk>':1}
        for h,r,t in triplets:
            vocab.renew_vocab([h,t],'etoi')

        numeralized_triplets = []
        for h,r,t in triplets:
            numeralized_triplets.append((vocab.etoi[h],vocab.rtoi[r],vocab.etoi[t]))
            assert vocab.rtoi[r] < 6702
        print()
        print('Filtered Entities: {}'.format(len(vocab.etoi)))
        print('Filtered Triplets: {}'.format(len(numeralized_triplets)))

        torch.save(vocab,args.vocab_pth)
        torch.save(numeralized_triplets,args.kb_triplets_pth)

    @staticmethod
    def collate_fn(list_of_examples):
        question = np.array(pad([x['question'] for x in list_of_examples],0))
        relation = [x['relation'] for x in list_of_examples]
        labels = [0 for i in range(len(relation))]

        kb_triplets_list = []
        for x in list_of_examples:
            kb_triplets_list.extend(x['kb_triplets'])
        kb_triplets_set = set(kb_triplets_list)
        rel_set = set()
        src,rel,tgt = [],[],[]
        for (h,r,t) in kb_triplets_set:
            rel_set.add(r)
            src.append(h)
            rel.append(r)
            tgt.append(t)

        src,rel,tgt = np.array(src),np.array(rel),np.array(tgt)
        uniq_v,edges = np.unique((src,tgt),return_inverse=True)
        src,dst = np.reshape(edges,(2,-1))
        num_rels = len(rel_set)
        g,rel,norm = build_graph_from_triplets(num_nodes=len(uniq_v),num_rels=num_rels,triplets=(src,rel,dst))
        # deg = g.in_degrees(range(g.number_of_nodes())).float().view(-1,1).to(device)

        return {
            'question':question,
            'relation':np.array(relation),
            'labels':np.array(labels),
            'uniq_v':uniq_v,
            'rel':rel,
            'norm':norm,
            'g':g
        }

    @staticmethod
    def load_dataset(args):
        vocab = torch.load(args.vocab_pth,pickle_module=dill)
        filepaths = ['train.tsv','dev.tsv','test.tsv']
        for i in range(len(filepaths)):
            filepaths[i] = os.path.join(args.data_dir,filepaths[i])

        train_dataset = SimpleQADataset(filepaths[0],vocab,args.batch_size,args.kb_triplets,args.ns)
        dev_dataset = SimpleQADataset(filepaths[1],vocab,args.batch_size,args.kb_triplets,ns=0)
        test_dataset = SimpleQADataset(filepaths[2],vocab,args.batch_size,args.kb_triplets,ns=0)

        return train_dataset, dev_dataset, test_dataset

    @staticmethod
    def load_vocab(args):
        return torch.load(args.vocab_pth)

    @staticmethod
    def load_graph(args):
        return torch.load(args.graph_pth)
