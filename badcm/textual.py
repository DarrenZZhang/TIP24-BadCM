import os
import random
import torch
import numpy as np
import torch.nn as nn
import torch.nn.functional as F
import badcm.modules.surrogate as surrogate
from tqdm import tqdm
from nltk import corpus
from transformers import BertConfig, BertTokenizer, AutoModelForMaskedLM
from badcm.modules.lazy_loader import LazyLoader
from dataset.dataset import TextMaskDataset
from dataset.dataset import get_dataset_filename
from utils.utils import check_path
from badcm.utils import get_poison_path


class GoalFunctionStatus(object):
    SUCCEEDED = 0  # attack succeeded
    SEARCHING = 1  # In process of searching for a success
    FAILED = 2 # attack failed


class GoalFunctionResult(object):
    goal_score = 1
    def __init__(self, text, score=0, similarity=None):
        self.status = GoalFunctionStatus.SEARCHING
        self.text = text
        self.score = score
        self.similarity = similarity
    
    @property
    def score(self):
        return self.__score
    
    @score.setter
    def score(self, value):
        self.__score = value
        if value >= self.goal_score:
            self.status = GoalFunctionStatus.SUCCEEDED


class TextualGenertor(object):
    """
    Generate poisoned texts

    Code Reference: 
        - BERT-Attack: https://github.com/LinyangLee/BERT-Attack
        - TextAttack: https://github.com/QData/TextAttack
    
    backdoor strategy
        - direct: replace important words directly
        - bert-attack: replace important words by [BERT-ATTACK](https://arxiv.org/abs/2004.09984)

    backdoor pattern:
        - random: select one important word randomly
        - all: replace all important words
        - sentence: replace all important words
    """
    
    cls_stop_words = set([
        ',', '.', '?', ':', ';', '!', '"', '\'', '-', '|', '/', '(', ')', '...', '।', '॥'
    ])

    cls_pattern_mode = ['direct', 'random', 'all', 'sentence']

    def __init__(self, cfg) -> None:
        self.cfg = cfg
        self.poison_path = get_poison_path(cfg, modal='texts')
        self.device = 'cuda' if len(cfg['device']) > 0 else 'cpu'
        self.strategy = cfg['backdoor']['strategy']
        self.max_text_len = cfg['max_text_len']
        self.max_candidate = cfg['max_candidate']
        self.bad_thred = cfg['bad_thred']
        self.semantic_thred = cfg['semantic_thred']
        GoalFunctionResult.goal_score = self.bad_thred

        self.stop_words = self.load_stop_words()
        self.pattern_word, self.pattern_mode = self.load_backdoor_pattern()

        if self.strategy != 'direct':
            self.poi_func = self._poison_by_bert_attack
            if cfg['enable_use']:
                # Universal Sentence Encoder: https://arxiv.org/abs/1803.11175
                # https://www.tensorflow.org/hub/tutorials/semantic_similarity_with_tf_hub_universal_encoder?hl=zh-cn
                hub = LazyLoader("tensorflow_hub", globals(), "tensorflow_hub")
                self.sentence_encoder = hub.load(self.cfg['use_path'])
            else:
                self.sentence_encoder = None

            mlm_path = self.cfg['mlm_path']
            self.tokenizer = BertTokenizer.from_pretrained(mlm_path, do_lower_case=True)
            
            bert_config = BertConfig.from_pretrained(mlm_path)
            self.mlm_model = AutoModelForMaskedLM.from_pretrained(mlm_path, config=bert_config)
            self.mlm_model.to(self.device)
            self.mlm_model.eval()
            
            surrogate_cfg = cfg['surrogate']
            surrogate_cls = getattr(surrogate, surrogate_cfg['model'])
            self.feature_extractor = surrogate_cls(surrogate_cfg['cfg'])

            self.feature_extractor.to(self.device)
            self.feature_extractor.eval()
        else:
            self.poi_func = self._poison_by_replacement_direct

    def load_backdoor_pattern(self):
        pattern_cfg = self.cfg['backdoor']
        pattern_mode = pattern_cfg['mode']
        assert pattern_mode in self.cls_pattern_mode

        if pattern_mode == 'sentence':
            pattern_word = pattern_cfg['sentence']
        else:
            pattern_word = pattern_cfg['word']
        
        return pattern_word, pattern_mode

    def load_stop_words(self):
        stop_words = corpus.stopwords.words('english')
        stop_words = set(stop_words)
        stop_words = stop_words.union(self.cls_stop_words)
        return stop_words

    def load_data(self, split):
        _, text_name, _ = get_dataset_filename(split)
        data_path = os.path.join(self.cfg['data_path'], self.cfg['dataset'])
        dataset = TextMaskDataset(data_path, text_name, mask_filename='badcm_{}_mask.npy'.format(split))
        return dataset

    def get_ref_text(self, text, mask):
        words = text.split(' ')

        mask_idx = np.where(mask == 1)[0]
        if self.pattern_mode == 'random':
            idx = random.randint(0, len(mask_idx) - 1)
            words[idx] = self.pattern_word
        elif self.pattern_mode == 'all':
            for idx in mask_idx:
                words[idx] = self.pattern_word
        elif self.pattern_mode == 'sentence':
            return self.pattern_word

        return ' '.join(words)
    
    def tokenize(self, text):
        words = text.split(' ')

        sub_words, keys = [], []
        index = 0
        for word in words:
            sub = self.tokenizer.tokenize(word)
            sub_words += sub
            keys.append([index, index + len(sub)])
            index += len(sub)
        return words, sub_words, keys

    def filter_substitutes(self, substitues):
        
        ret = []
        for word in substitues:
            if word.lower() in self.stop_words:
                continue
            if '##' in word:
                continue

            ret.append(word)
        return ret
    
    def get_bpe_substitutes(self, substitutes):
        # substitutes L, k
        substitutes = substitutes[0:12, 0:4] # maximum BPE candidates

        # find all possible candidates 
        all_substitutes = []
        for i in range(substitutes.size(0)):
            if len(all_substitutes) == 0:
                lev_i = substitutes[i]
                all_substitutes = [[int(c)] for c in lev_i]
            else:
                lev_i = []
                for all_sub in all_substitutes:
                    for j in substitutes[i]:
                        lev_i.append(all_sub + [int(j)])
                all_substitutes = lev_i

        # all substitutes: list of list of token-id (all candidates)
        cross_entropy_loss = nn.CrossEntropyLoss(reduction='none')
        
        word_list = []
        all_substitutes = torch.tensor(all_substitutes) # [ N, L ]
        all_substitutes = all_substitutes[:24].to(self.device)

        N, L = all_substitutes.size()
        word_predictions = self.mlm_model(all_substitutes)[0] # N L vocab-size
        ppl = cross_entropy_loss(word_predictions.view(N*L, -1), all_substitutes.view(-1)) # [ N*L ] 
        ppl = torch.exp(torch.mean(ppl.view(N, L), dim=-1)) # N  

        _, word_list = torch.sort(ppl)
        word_list = [all_substitutes[i] for i in word_list]
        final_words = []
        for word in word_list:
            tokens = [self.tokenizer.convert_ids_to_tokens(int(i)) for i in word]
            text = ' '.join([t.strip() for t in tokens])
            final_words.append(text)
        return final_words

    def get_substitutes(self, substitutes, substitutes_score, threshold=3.0):
        ret = []
        num_sub, _ = substitutes.size()
        
        if num_sub == 0:
            return ret
        elif num_sub == 1:
            for id, score in zip(substitutes[0], substitutes_score[0]):
                if threshold != 0 and score < threshold:
                    break
                ret.append(self.tokenizer.convert_ids_to_tokens(int(id)))
        else:
            ret = self.get_bpe_substitutes(substitutes)

        return ret

    def get_transformations(self, text, idx, substitutes):
        words = text.split(' ')

        trans_text = []
        for sub in substitutes:
            words[idx] = sub
            trans_text.append(' '.join(words))
        return trans_text

    def get_text_similarity(self, trans_texts, ori_text):
        if self.sentence_encoder is None:
            return [None] * len(trans_texts)
        
        encoding = self.sentence_encoder([ori_text] + trans_texts)

        if isinstance(encoding, dict):
            encoding = encoding["outputs"]
        
        encoding = encoding.numpy()
        encoding = torch.tensor(encoding)
        ori_encoding = encoding[0].unsqueeze(0)
        trans_encoding = encoding[1:]

        sim = F.cosine_similarity(ori_encoding.repeat(trans_encoding.size(0), 1), trans_encoding)
        sim = sim.numpy()
        return sim

    def get_goal_results(self, trans_texts, ori_text, ref_text):
        text_batch = [ref_text] + trans_texts
        with torch.no_grad():
            feats = self.feature_extractor(text_batch, device=self.device)
            feats = feats.flatten(start_dim=1)

        ref_feats = feats[0].unsqueeze(0)
        trans_feats = feats[1:]
        cos_sim = F.cosine_similarity(ref_feats.repeat(trans_feats.size(0), 1), trans_feats)
        cos_sim = cos_sim.cpu().numpy()

        text_sim = self.get_text_similarity(trans_texts, ori_text)

        results = []
        for i in range(len(trans_texts)):
            if text_sim[i] is not None and text_sim[i] < self.semantic_thred:
                continue
            results.append(GoalFunctionResult(trans_texts[i], score=cos_sim[i], similarity=text_sim[i]))
        return results

    def attak(self, text, mask, ref_text):
        words, sub_words, keys = self.tokenize(text)

        inputs = self.tokenizer(text, add_special_tokens=True, max_length=self.max_text_len, 
                                truncation=True, return_tensors="pt")
        input_ids = inputs["input_ids"].to(self.device)

        with torch.no_grad():
            word_predictions = self.mlm_model(input_ids)['logits'].squeeze() # (seq_len, vocab_size)

        word_pred_scores_all, word_predictions = torch.topk(word_predictions, self.max_candidate, -1)

        word_predictions = word_predictions[1:-1, :]  # remove [CLS] and [SEP]
        word_pred_scores_all = word_pred_scores_all[1:-1, :]

        # Greedy Search
        cur_result = GoalFunctionResult(text)
        mask_idx = np.where(mask == 1)[0]
        
        for idx in mask_idx:
            predictions = word_predictions[keys[idx][0]: keys[idx][1]]
            predictions_socre = word_pred_scores_all[keys[idx][0]: keys[idx][1]]
            substitutes = self.get_substitutes(predictions, predictions_socre)
            substitutes = self.filter_substitutes(substitutes)

            trans_texts =  self.get_transformations(cur_result.text, idx, substitutes)
            if len(trans_texts) == 0:
                continue

            results = self.get_goal_results(trans_texts, text, ref_text)
            results = sorted(results, key=lambda x: x.score, reverse=True)
            
            if len(results) > 0 and results[0].score > cur_result.score:
                cur_result = results[0]
            else:
                continue

            if cur_result.status == GoalFunctionStatus.SUCCEEDED:
                max_similarity = cur_result.similarity
                if max_similarity is None:
                    # similarity is not calculated
                    continue

                for result in results[1:]:
                    if result.status != GoalFunctionStatus.SUCCEEDED:
                        break
                    if result.similarity > max_similarity:
                        max_similarity = result.similarity
                        cur_result = result
                return cur_result
        
        if cur_result.status == GoalFunctionStatus.SEARCHING:
            cur_result.status = GoalFunctionStatus.FAILED
        
        return cur_result

    def _poison_by_bert_attack(self, dataset):
        print("Poison strategy: Bert-Attack")
        
        poi_texts = []
        poi_scores = []
        success_rate = 0
        for i, data in enumerate(tqdm(dataset)):
            text, mask = data
            ref_text = self.get_ref_text(text, mask)
            attack_result = self.attak(text, mask, ref_text)
            
            if attack_result.status == GoalFunctionStatus.SUCCEEDED:
                success_rate += 1

            poi_scores.append((i, attack_result.score))
            poi_texts.append(attack_result.text)
        
        success_rate /= len(dataset)
        print("attack success rate: {:.2f}".format(success_rate))
        return poi_texts, poi_scores
    
    def _poison_by_replacement_direct(self, dataset):
        print("Poison strategy: Replacement by pattern word")
        poi_texts = []
        for text, mask in tqdm(dataset):
            ref_text = self.get_ref_text(text, mask)
            poi_texts.append(ref_text)
        return poi_texts, None

    def generate_poisoned_texts(self, split):
        dataset = self.load_data(split)
        poi_texts, poi_scores = self.poi_func(dataset)

        # save poisoned texts
        save_path = os.path.join(self.cfg['data_path'], self.cfg['dataset'], self.poison_path)
        _, text_name, _ = get_dataset_filename(split)
        check_path(save_path, isdir=True)

        # save text idx by 
        if poi_scores:
            poi_scores = sorted(poi_scores, key=lambda x: x[1], reverse=True)
            poi_idx = [i[0] for i in poi_scores]
            np.save(os.path.join(save_path, text_name.replace('.txt', '.npy')), np.array(poi_idx))

        with open(os.path.join(save_path, text_name), 'w') as f:
            f.writelines([text + '\n' for text in poi_texts])


def run(cfg):
    module = TextualGenertor(cfg)

    print("Generating poisoned text for test dataset...")
    module.generate_poisoned_texts('test')
    print("Generating poisoned text for train dataset...")
    module.generate_poisoned_texts('train')
