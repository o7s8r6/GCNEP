from utils.util import pad
import numpy as np
class Vocab(object):
    def __init__(self):
        pass

    def renew_vocab(self,data,name):
        for d in data:
            if d in getattr(self,name):
                continue
            else:
                getattr(self,name)[d] = len(getattr(self,name))


class SimpleQAVocab(Vocab):

    def __init__(self):
        self.relIdx2wordIdx = {}

    def get_all_relation_words(self):
        n_relations = len(self.rtoi)
        return np.array(pad([self.relIdx2wordIdx[i] for i in range(n_relations)],0,max_len=20))
