import os
from data.twitter_data import data
from data.twitter_data.data import SOS,EOS,UNK
import data_utils
import torch
import torch.nn as nn
from collections import Counter
import numpy as np
import random
from torch import optim
import torch.nn.functional as F
config ={}
config['twitter_datapath'] = 'data/twitter_data/'
# config['vocab_size']
config['emb_dim'] = 300
config['hid_size'] = 1024
config['num_layers'] =3
config['num_epoch'] = 50
config['batch_size'] = 32
config['teaching'] = False
config['teacher_forcing_ratio'] = 1
config['decoder_lr']= 0.0005
config['learning rate'] = 0.0001
config['max_len'] = 20
config['bidirectional']  = True
config['attn'] = False
config['attn_model'] = 'concat'
config['model_dir']= 'save/'


metadata, idx_q, idx_a = data.load_data(config['twitter_datapath'])
glove_emb = np.load('data/glove_emb.npy')
glove_emb = torch.tensor(glove_emb).float()
SOS_token = metadata['w2idx'][SOS]
EOS_token = metadata['w2idx'][EOS]
UNK_token = metadata['w2idx'][UNK]
PAD_token = 0
config['vocab_size'] = len(metadata['idx2w'])

criterion  = nn.CrossEntropyLoss(ignore_index = EOS_token)
(trainX, trainY), (testX, testY), (validX, validY) = data_utils.split_dataset(idx_q, idx_a)
use_cuda = torch.cuda.is_available()


# Setup GPU optimization if CUDA is supported
if use_cuda:
    computing_device = torch.device("cuda")
    extras = {"num_workers": 4, "pin_memory": True}
    print("CUDA is supported")
else: # Otherwise, train on the CPU
    computing_device = torch.device("cpu")
    extras = False
    print("CUDA NOT supported")
    
# glove_emb = glove_emb.to(computing_device)
class Encoder(nn.Module):
    def __init__(self, config):
        super(Encoder, self).__init__()
        self.input_size = config['vocab_size']
        self.emb_dim  = config['emb_dim']
        self.hidden_size = config['hid_size']
        self.num_layers = config['num_layers']
        self.time_step = config['max_len']
#         self.embedding = nn.Embedding.from_pretrained(glove_emb)
        self.embedding = nn.Embedding(self.input_size,self.emb_dim)
        self.embedding.weight = nn.Parameter(glove_emb)
        
#         self.embedding.load_state_dict({'weight': glove_emb})
        self.bidire = config['bidirectional'] 
        self.rnn = nn.GRU(self.emb_dim, self.hidden_size,num_layers=self.num_layers,batch_first = True,bidirectional= self.bidire)
      
    def forward(self,input_data,lengths,hidden = None):
        time_step = config['max_len']
        embedded = self.embedding(input_data)
        embedded = nn.utils.rnn.pack_padded_sequence(embedded, lengths,batch_first = True)
        output = embedded
        output, hidden = self.rnn(output,hidden)
        output,ouput_lengths = nn.utils.rnn.pad_packed_sequence(output,batch_first = True)
        if self.bidire:
            output = output[:, :, :self.hidden_size] + output[:, : ,self.hidden_size:]
        return output,hidden[:self.num_layers]
    
    def init_hidden(self):
        hidden = torch.zeros(1,self.time_step , self.hidden_size, device=computing_device)
        return hidden
    
class Attn(torch.nn.Module):
    def __init__(self, method, hidden_size):
        super(Attn, self).__init__()
        self.method = method
        if self.method not in ['dot', 'general', 'concat']:
            raise ValueError(self.method, "is not an appropriate attention method.")
        self.hidden_size = hidden_size
        if self.method == 'general':
            self.attn = torch.nn.Linear(self.hidden_size, hidden_size)
        elif self.method == 'concat':
            self.attn = torch.nn.Linear(self.hidden_size * 2, hidden_size)
            self.v = torch.nn.Parameter(torch.FloatTensor(hidden_size))

    def dot_score(self, hidden, encoder_output):
        return torch.sum(hidden * encoder_output, dim=2)

    def general_score(self, hidden, encoder_output):
        energy = self.attn(encoder_output)
        return torch.sum(hidden * energy, dim=2)

    def concat_score(self, hidden, encoder_output):
        energy = self.attn(torch.cat((hidden.unsqueeze(1).expand(-1, encoder_output.size(1), -1), encoder_output), 2)).tanh()
        return torch.sum(self.v * energy, dim=2)

    def forward(self, hiddens, encoder_outputs):
        # Calculate the attention weights (energies) based on the given method
        batch_size,de_time_step = hiddens.shape[0],hiddens.shape[1]
        en_time_step = encoder_outputs.shape[1]
        attn_energies = torch.zeros(batch_size,de_time_step,en_time_step).to(computing_device)
        for i in range(de_time_step):
            if self.method == 'general':
                attn_energies[:,i,:] = self.general_score(hiddens[:,i,:], encoder_outputs)
            elif self.method == 'concat':
                attn_energies[:,i,:] = self.concat_score(hiddens[:,i,:], encoder_outputs)
            elif self.method == 'dot':
                attn_energies[:,i,:] = self.dot_score(hiddens[:,i,:], encoder_outputs)
        return F.softmax(attn_energies, dim=2)

class Decoder(nn.Module):
    def __init__(self, config):
        super(Decoder, self).__init__()
        self.input_size = config['vocab_size']
        self.output_size = self.input_size
        self.emb_dim  = config['emb_dim']
        self.num_layers = config['num_layers']
        self.hidden_size = config['hid_size']
        self.attn = config['attn']
        self.embedding = nn.Embedding(self.input_size,self.emb_dim)
        self.embedding.weight = nn.Parameter(glove_emb)
#         self.embedding = nn.Embedding(self.input_size, self.emb_dim)
#         self.embedding.load_state_dict({'weight': glove_emb})
        
        self.rnn = nn.GRU(self.emb_dim, self.hidden_size,num_layers=self.num_layers,batch_first = True)
        self.out = nn.Linear(self.hidden_size, self.output_size)
        self.attn_layer = Attn(config['attn_model'], self.hidden_size)
        self.concat = nn.Linear(self.hidden_size * 2, self.hidden_size)
        self.softmax = nn.LogSoftmax(dim=1)
        
    def forward(self, input_data, hidden,encoder_outputs):
        batch_size = input_data.shape[1]
        time_step = input_data.shape[0]
        
        output = self.embedding(input_data)
        output = F.relu(output)
        output, hidden = self.rnn(output, hidden)
        if self.attn:
            attn_weights = self.attn_layer(output, encoder_outputs)
            context = attn_weights.bmm(encoder_outputs)
            # Concatenate weighted context vector and GRU output using 
            concat_input = torch.cat((output, context), 2)
            concat_output = torch.tanh(self.concat(concat_input))
           
        output = self.out(output)
        return output,hidden

    def init_hidden(self,hidden):
        return hidden

def batchBLEU(output,target):
    score = 0.0
    batch_size = len(target)
    output= output.detach().cpu().numpy()
    target= target.cpu().numpy()
    for i in range(batch_size):
        score+=seqBLEU(output[i],target[i])
    return score/batch_size
    
def seqBLEU(candidate,reference):
    reference = reference.tolist()
    endidx = reference.index(EOS_token)
    reference = reference[1:endidx]
    counts = Counter(candidate)
    if not counts:
        return 0
    max_counts = {}
    reference_counts = Counter(reference)
    for ngram in counts:
        max_counts[ngram] = max(max_counts.get(ngram, 0), reference_counts[ngram])
    clipped_counts = dict((ngram, min(count, max_counts[ngram])) for ngram, count in counts.items())
    return sum(clipped_counts.values()) / sum(counts.values())
    
def train():
    modelname = ''
    if config['attn']:
        print("Here we will us attention")
        modelname = 'att'+'_'+config['attn_model']
    encoder_path= config['model_dir']+'encoder_'+modelname+'.ckpt'
    decoder_path= config['model_dir']+'decoder_'+modelname+'.ckpt'
    
    encoder = Encoder(config)
    decoder = Decoder(config)
    encoder.to(computing_device)
    decoder.to(computing_device)
    encod_emb_params = list(map(id, encoder.embedding.parameters()))
    encod_base_params =  filter(lambda p: id(p) not in encod_emb_params,encoder.parameters())

    encoder_optimizer = torch.optim.Adam([{'params': encod_base_params},
                                          {'params': encoder.embedding.parameters(),'lr': config['learning rate']/100}],
                                          lr = config['learning rate'])
    
    decod_emb_params = list(map(id, decoder.embedding.parameters()))
    decod_base_params = filter(lambda p: id(p) not in decod_emb_params,decoder.parameters())
    
    decoder_optimizer = torch.optim.Adam([{'params': decod_base_params},
                                          {'params': decoder.embedding.parameters(),'lr': config['decoder_lr']/100}],
                                          lr = config['decoder_lr'])
#     encoder_optimizer = torch.optim.Adam(encoder.parameters(),lr = config['learning rate'])
#     decoder_optimizer = torch.optim.Adam(decoder.parameters(),lr = config['decoder_lr'])
    
    min_val_loss=100
    for epoch in range(config['num_epoch']):
        _,train_loss = run_epoch(encoder,decoder,trainX,trainY,training=True,encoder_optimizer=encoder_optimizer,\
                                 decoder_optimizer = decoder_optimizer)
        
        bleu,val_loss = run_epoch(encoder,decoder,validX,validY,training=False,encoder_optimizer=encoder_optimizer,\
                                 decoder_optimizer = decoder_optimizer)
#         if val_loss<min_val_loss:
#             min_val_loss=val_loss
        torch.save(encoder.state_dict(),encoder_path)
        torch.save(decoder.state_dict(),decoder_path)
        if epoch%5==0: 
            evaluate_test(encoder,decoder)
        print('Epoch %d,training loss: %.3f, validation loss:%.3f, BLEU score is:%.3f '%(epoch + 1, train_loss,val_loss,bleu))
    
    test_bleu,test_loss = run_epoch(encoder,decoder,testX,testY,training=False,encoder_optimizer=encoder_optimizer,\
                                 decoder_optimizer = decoder_optimizer)
    print('Training completed after %d epochs, BLEU score is %.3f'%(epoch+1,test_bleu))
#     print(test_bleu,test_loss)
    return

def run_epoch(encoder,decoder,feature,labels,training = False,encoder_optimizer=None,decoder_optimizer =None):
    
    batch_size = config['batch_size']
    epoch_loss = 0
    epoch_bleu = 0
    N = 1000
    N_minibatch_loss =0.0
    data_loader = data_utils.batch_generator(feature,labels,batch_size = config['batch_size'])
    for minibatch_count, (data_len,labels) in enumerate(data_loader):
        
        if training:
            encoder_optimizer.zero_grad()
            decoder_optimizer.zero_grad()
        data,lengths = data_len
        data,lengths,labels = torch.tensor(data,dtype=torch.long),torch.tensor(lengths,dtype=torch.long),torch.tensor(labels,dtype=torch.long)
        data,labels = data.to(computing_device),labels.to(computing_device)
        
        encoder_outputs, encoder_hidden = encoder(data,lengths)
        
#         print(encoder_hidden.shape)
#         assert 0==1
        decoder_hidden = encoder_hidden
        max_target_len = config['max_len']
        loss = 0
#         when training, 50% to teacher_forcing
        
        if training:
            use_teacher_forcing = True if random.random() < config['teacher_forcing_ratio'] else False
        
        #when test or valid, don't use teacher_forcing
        else: 
            use_teacher_forcing = False
        
        if config['teaching']:
            use_teacher_forcing = True
        
        decoder_charid = torch.zeros_like(labels)
        if use_teacher_forcing:
            decoder_charid[:,0]= torch.LongTensor([[SOS_token for _ in range(batch_size)]]).to(computing_device).reshape(-1)
            
            batch_size = labels.shape[0]
            target = labels[:,1:]
            target = target.contiguous().view(-1)
            
            decoder_input = labels[:,:-1]
            decoder_output, decoder_hidden = decoder(decoder_input, decoder_hidden, encoder_outputs)
            
            decoder_charid[:,1:]= torch.argmax(decoder_output,dim=2)
            decoder_output = decoder_output.view(-1,config['vocab_size'])
            loss = criterion(decoder_output,target)
        else:
            decoder_input = torch.LongTensor([[SOS_token for _ in range(batch_size)]]).to(computing_device).transpose(0,1)
            decoder_charid[:,0] = decoder_input.reshape(-1)
            batch_size = labels.shape[0]
#             print(labels[:1])
            target = labels[:,1:]
            for t in range(max_target_len-1):
#                 print(decoder_input[:1],target[:1,t])
                decoder_output, decoder_hidden = decoder(decoder_input, decoder_hidden, encoder_outputs)
                output_id = torch.argmax(decoder_output.detach(),dim=2)
                decoder_charid[:,t+1] = output_id.squeeze()
                decoder_input = output_id
                loss += criterion(decoder_output.squeeze(), target[:,t])
#             loss = criterion(decoder_charid[:,1:],target)
            loss /= lengths.float().mean()
        if training:
            loss.backward()
            nn.utils.clip_grad_norm(encoder.parameters(), 50)
            nn.utils.clip_grad_norm(decoder.parameters(), 50)
            encoder_optimizer.step()
            decoder_optimizer.step()
                
#         print(decoder_charid[:1])        
#         assert 0==1
        epoch_bleu += batchBLEU(decoder_charid,labels)
        epoch_loss+=loss.detach() 
        N_minibatch_loss+=loss.detach()
#         loss = 0
#         if minibatch_count >5000:
#             print(minibatch_count)
        if (minibatch_count%N ==0) and (minibatch_count!=0):
#             print('hhahahahah',minibatch_count)
            train_flag = "Training" if training else "Validating/Testing"
            print(train_flag+' Average minibatch %d loss: %.3f'%(minibatch_count, N_minibatch_loss/N ))
            N_minibatch_loss = 0
            
    return epoch_bleu/minibatch_count,epoch_loss/minibatch_count


def evaluate_test(encoder,decoder):
    modelname = ''
    if config['attn']:
        modelname = 'att'+'_'+config['attn_model']
        
    data_loader = data_utils.batch_generator(testX,testY,batch_size = config['batch_size'],shuffle = False)
    data_len,labels = next(data_loader)
    data,lengths  = data_len
    data,lengths,labels = torch.tensor(data,dtype=torch.long),torch.tensor(lengths,dtype=torch.long),torch.tensor(labels,dtype=torch.long)
    data,labels = data.to(computing_device),labels.to(computing_device)
    encoder_outputs, encoder_hidden = encoder(data,lengths)
    decoder_hidden = encoder_hidden
    max_target_len = config['max_len']
    loss = 0
    decoder_charid = torch.zeros_like(labels)
    batch_size = labels.shape[0]
    decoder_input = torch.LongTensor([[SOS_token for _ in range(batch_size)]]).to(computing_device).transpose(0,1)
    decoder_charid[:,0] = decoder_input.reshape(-1)
    
    target = labels[:,1:]
    for t in range(max_target_len-1):
        decoder_output, decoder_hidden = decoder(decoder_input, decoder_hidden, encoder_outputs)
        output_id = torch.argmax(decoder_output.detach(),dim=2)
        decoder_charid[:,t+1] = output_id.squeeze()
        decoder_input = output_id
        
    data,decoder_charid,labels=data.cpu().numpy().tolist(),decoder_charid.cpu().numpy().tolist(),labels.cpu().numpy().tolist()
    ori_input = []
    model_output = []
    target_output = []
    for i,sentence_id in enumerate(decoder_charid):
        condition = lambda t: t not in (PAD_token,EOS_token,SOS_token)
        
        input_sentence_id = list(filter(condition,data[i]))
        input_sentence = ' '.join([metadata['idx2w'][idx] for idx in input_sentence_id])
        ori_input.append(input_sentence)
        sentence_id = list(filter(condition,sentence_id))
        sentence=' '.join([metadata['idx2w'][idx] for idx in sentence_id ])
        target_sentence_id = list(filter(condition,labels[i]))
        model_output.append(sentence)
        target_sentence = ' '.join([metadata['idx2w'][idx] for idx in target_sentence_id])
        target_output.append(target_sentence)
    filename  = 'log/'+modelname+'result.txt'
    with open('log/result.txt','a') as f:
        for i,sentence in enumerate(model_output):
            f.write("Input:"+ ori_input[i]+ '\n'+ 'Chatbot:'+ sentence +'\n \n')
if __name__ == "__main__":
    train()