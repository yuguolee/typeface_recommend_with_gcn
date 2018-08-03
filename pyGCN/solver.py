import os
import numpy as np
import time
import datetime
import torch
import torchvision
from torch import optim
from torch.autograd import Variable
import torch.nn.functional as F
from model import *
from utils import *

class Solver(object):
	def __init__(self, config, train_loader, test_loader):

		# Data loader
		self.train_loader = train_loader
		self.test_loader = test_loader

		# Models
		self.text_encoder = None
		self.image_encoder = None
		self.joint_model = None

		# Models hyper-parameters
		self.image_size = config.image_size
		self.word_dim = config.word_dim
		self.z_dim = config.z_dim
		self.num_typo = config.num_typo

		# Hyper-parameters
		self.text_maxlen = config.text_maxlen
		self.lr = config.lr
		self.beta1 = config.beta1
		self.beta2 = config.beta2
		self.dropout = config.dropout
		self.lambda_cls = config.lambda_cls

		# Training settings
		self.start_epochs = config.start_epochs
		self.sample_epochs = config.sample_epochs
		self.num_epochs = config.num_epochs
		self.num_epochs_decay = config.num_epochs_decay
		self.batch_size = config.batch_size
		self.weight_decay = config.weight_decay

		# Step size
		self.log_step = config.log_step
		self.val_step = config.val_step

		# Path
		self.sample_path = config.sample_path
		self.model_path = config.model_path
		self.result_path = config.result_path

		# GCN Dataset
		self.adj = torch.from_numpy(np.load(config.adj_path)).type(torch.FloatTensor)
		self.adj = normalize(self.adj + torch.eye(self.adj.shape[0]))
		self.image_loader = get_loader(config.data_path, config.img_path, self.image_size, self.batch_size)
		self.idx_train = range(int(len(self.image_loader)*0.8))
		self.idx_val   = range(int(len(self.image_loader)*0.8), len(self.image_loader))

		self.device = torch.device('cuda' if torch.cuda.is_available() else 'cpu')

		self.build_model()

	def build_model(self):
		self.text_encoder = Text_Encoder(self.word_dim, self.z_dim, self.text_maxlen)
		self.image_encoder = Resnet(self.z_dim, self.num_typo, self.image_size)
		self.joint_model = 	GCN(nfeat=self.z_dim, nhid=self.z_dim//2, nclass=int(self.num_typo), dropout=self.dropout)
		self.optimizer = optim.Adam(list(filter(lambda p: p.requires_grad, self.text_encoder.parameters())) + \
									list(self.image_encoder.parameters()) + list(self.joint_model.parameters()),
									self.lr, [self.beta1, self.beta2], weight_decay=self.weight_decay)

		if torch.cuda.is_available():
			self.text_encoder.cuda()
			self.image_encoder.cuda()
			self.joint_model.cuda()
			self.adj = Variable(self.adj).type(torch.cuda.FloatTensor)
		self.print_network(self.text_encoder, 'TE')
		self.print_network(self.image_encoder, 'IE')
		self.print_network(self.joint_model, 'JM')

	def print_network(self, model, name):
		"""Print out the network information."""
		num_params = 0
		for p in model.parameters():
			num_params += p.numel()
		print(model)
		print(name)
		print("The number of parameters: {}".format(num_params))


	def reset_grad(self):
		"""Zero the gradient buffers."""
		self.text_encoder.zero_grad()
		self.image_encoder.zero_grad()

	def restore_model(self, resume_iters):
		"""Restore the trained generator and discriminator."""
		te_path = os.path.join(self.model_path, 'text-encoder-%d.pkl' %(resume_iters))
		ie_path = os.path.join(self.model_path, 'image-encoder-%d.pkl' %(resume_iters))
		if os.path.isfile(te_path) and os.path.isfile(ie_path):
			self.text_encoder.load_state_dict(torch.load(te_path, map_location=lambda storage, loc: storage))
			self.image_encoder.load_state_dict(torch.load(ie_path, map_location=lambda storage, loc: storage))
			print('te/se is Successfully Loaded from %d epoch'%resume_iters)
			return resume_iters
		else:
			return 0


	def train(self):

		start_iter = self.restore_model(self.start_epochs)
		iters_per_epoch = len(self.train_loader)
		start_time = time.time()

		for epoch in range(start_iter, self.num_epochs):
			text_acc = 0.
			img_acc  = 0.
			#======================================= Train =======================================#
			for i, (typography, pos_image, neg_image, text, text_len, text_typo_label) in enumerate(self.train_loader):
				loss = {}
				typography = typography.to(self.device)
				pos_image = pos_image.to(self.device)
				neg_image = neg_image.to(self.device)
				text = text.to(self.device)
				text_len = text_len.to(self.device)
				text_typo_label = text_typo_label.type(torch.cuda.FloatTensor)

				# Text Embedding
				# (batch x z_dim)
				text_emb, text_cls  = self.text_encoder(text, text_len)
				loss_text_cls = F.binary_cross_entropy_with_logits(text_cls, text_typo_label)

				# Image Embedding
				# (batch x z_dim), (batch x z_dim)
				_	  , pos_style_emb, out_cls = self.image_encoder(pos_image)
				_	  , neg_style_emb, _	   = self.image_encoder(neg_image)
				loss_img_cls = F.cross_entropy(out_cls, typography)

				# Joint Embedding
				#loss_triplet = F.triplet_margin_loss(text_emb, pos_style_emb, neg_style_emb, margin=1)

				# GAN / OCR


				# Accuracy
				text_acc+= baccuracy_at_k(text_cls, text_typo_label)
				img_acc += accuracy_at_k(out_cls, typography)

				# Compute gradient penalty
				total_loss = loss_text_cls + loss_img_cls

				# Backprop + Optimize
				self.reset_grad()
				total_loss.backward()
				self.optimizer.step()

				# logging
				loss['text_cls']= loss_text_cls.item()
				#loss['triplet'] = loss_triplet.item()
				#loss['img_l1']  = loss_img_l1.item()
				loss['img_cls']	= loss_img_cls.item()

				# Print the log info
				if (i+1) % self.log_step == 0:
					elapsed = time.time() - start_time
					elapsed = str(datetime.timedelta(seconds=elapsed))

					log = "Elapsed [{}], Epoch [{}/{}], Iter [{}/{}]".format(
							elapsed, epoch+1, self.num_epochs, i+1, iters_per_epoch)
					log += ", text prec@30: {:.4f}".format(text_acc/(i+1))
					log += ", image prec@5: {:.4f}".format(img_acc/(i+1))

					for tag, value in loss.items():
						log += ", {}: {:.4f}".format(tag, value)
					print(log)

			#======================================== GCN =======================================#

			# Image Embedding
			features = []
			labels = []
			for i, (label, image) in enumerate(self.image_loader):
				image = Variable(image).cuda()
				label = Variable(label).cuda()

				_, style_emb, _ = self.image_encoder(image)
				features.append(style_emb)
				labels.append(label)
			features = torch.cat(features, 0)
			labels = torch.cat(labels, 0)

			# Joint Embedding
			output = self.joint_model(features, self.adj)
			loss_train = F.cross_entropy(output[self.idx_train], labels[self.idx_train])
			acc_train = accuracy_at_k(output[self.idx_train], labels[self.idx_train])

			self.optimizer.zero_grad()
			loss_train.backward()
			self.optimizer.step()

			print('[GCN]Epoch: {:04d}'.format(epoch+1),
				  'loss_train: {:.4f}'.format(loss_train.item()),
				  'acc@5_train: {:.4f}'.format(acc_train))

			#==================================== Validation ====================================#
			if (epoch+1) % self.val_step == 0:
				self.text_encoder.train(False)
				self.image_encoder.train(False)
				self.text_encoder.eval()
				self.image_encoder.eval()

				val_loss = 0.
				text_acc = 0.
				img_acc  = 0.
				for i, (typography, image, _, text, text_len, text_typo_label) in enumerate(self.test_loader):
					typography = typography.to(self.device)
					image = image.to(self.device)
					text = text.to(self.device)
					text_len = text_len.to(self.device)
					text_typo_label = text_typo_label.type(torch.cuda.FloatTensor)

					# (batch x z_dim)
					text_emb, text_cls  = self.text_encoder(text, text_len)
					# (batch x z_dim), (batch x z_dim)
					out_img, style_emb, out_cls = self.image_encoder(image)

					val_loss+= torch.mean((text_emb - style_emb) ** 2).item()

					text_acc+= baccuracy_at_k(text_cls, text_typo_label)
					img_acc += accuracy_at_k(out_cls, typography)

				print('Val Loss: {:.4f}, Text Acc@30: {:.4f}, Image Acc@5: {:.4f}'\
					  .format(val_loss/(i+1), text_acc/(i+1), img_acc/(i+1)))

				te_path = os.path.join(self.model_path, 'text-encoder-%d.pkl' %(epoch+1))
				ie_path = os.path.join(self.model_path, 'image-encoder-%d.pkl' %(epoch+1))
				torch.save(self.text_encoder.state_dict(), te_path)
				torch.save(self.image_encoder.state_dict(), ie_path)

				self.text_encoder.train(True)
				self.image_encoder.train(True)

	def sample(self):

		self.restore_model(self.sample_epochs)
		word2id = np.load("./data/word_emb/gb_w2i.npy").item()
		typo_list = np.load('./data/typo_list.npy')

		cl_typo = None
		cl_dist = -1

		anch = 'fancy car'.split(' ')
		anch_word = [word2id[word] for word in anch]
		anch_word+= [1]*(self.text_maxlen-len(anch_word))
		anch_word = Variable(torch.from_numpy(np.asarray(anch_word))).cuda().unsqueeze(0)
		anch_len = Variable(torch.from_numpy(np.asarray([len(anch)]))).cuda()

		text_acc = 0.
		with torch.no_grad():
			anch_vec, anch_cls = self.text_encoder(anch_word, anch_len)

			for i, (typography, image, _, text, text_len, text_typo_label) in enumerate(self.test_loader):
				image = image.to(self.device)
				text = text.to(self.device)
				text_len = text_len.to(self.device)

				text_emb, text_cls  = self.text_encoder(text, text_len)
				out_img, style_emb, out_cls = self.image_encoder(image)

				# TASK 1 : Paragraph to Typography classification
				_, pred  = torch.sort(text_cls, 1, descending=True)
				text_acc+= baccuracy_at_k(pred.data.cpu().numpy(), text_typo_label)

				# TASK 2 : The Closest Words from a Typography Image

				# TASK 3 : The Closest Typographies from a Word
				# (batch_size)
				mm = F.cosine_similarity(anch_vec, style_emb)
				max_value, max_idx = torch.max(mm.unsqueeze(0), 1)
				if cl_dist < max_value[0]:
					cl_dist = max_value[0]
					cl_typo = typo_list[typography[max_idx[0]].numpy()]
					print(cl_dist.cpu().numpy(), cl_typo)

				# TASK 4 : Visualization

			# Results
			print('TASK 1 : Text Acc@30: {:.4f}'.format(text_acc/(i+1)))
			print('TASK 3 : The Closest Typo with {} is {}'.format(anch, cl_typo))