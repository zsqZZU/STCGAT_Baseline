import torch
import math
import os
import time
import copy
import numpy as np
from lib.logger import get_logger
from lib.metrics import All_Metrics
from lib.TrainInits import print_model_parameters
from sklearn.metrics import mean_absolute_error
from sklearn.metrics import mean_squared_error
from model.metrics import masked_mape_np
import xlwt


class Trainer(object):
    def __init__(self, model, vector_field_f, vector_field_g, loss, optimizer, train_loader, val_loader, test_loader,
                 mean_, std_, args, lr_scheduler, device, times,
                 w):
        super(Trainer, self).__init__()
        self.model = model
        self.vector_field_f = vector_field_f 
        self.vector_field_g = vector_field_g
        self.loss = loss
        self.optimizer = optimizer
        self.train_loader = train_loader
        self.val_loader = val_loader
        self.test_loader = test_loader
        self.mean_ = mean_
        self.std_ = std_
        self.args = args
        self.lr_scheduler = lr_scheduler
        self.train_per_epoch = len(train_loader)
        if val_loader != None:
            self.val_per_epoch = len(val_loader)
        self.best_path = os.path.join(self.args.log_dir, 'best_model.pth')
        self.loss_figure_path = os.path.join(self.args.log_dir, 'loss.png')
        #log
        if os.path.isdir(args.log_dir) == False and not args.debug:
            os.makedirs(args.log_dir, exist_ok=True)
        self.logger = get_logger(args.log_dir, name=args.model, debug=args.debug)
        self.logger.info('Experiment log path in: {}'.format(args.log_dir))
        total_param = print_model_parameters(model, only_num=False)
        for arg, value in sorted(vars(args).items()):
            self.logger.info("Argument %s: %r", arg, value)
        self.logger.info(self.model)
        self.logger.info("Total params: {}".format(str(total_param)))
        self.device = device
        self.times = times.to(self.device, dtype=torch.float)
        self.w = w

    def val_epoch(self, epoch, val_dataloader):
        self.model.eval()
        total_val_loss = 0

        with torch.no_grad():
            for batch_idx, batch in enumerate(self.val_loader):
            # for iter, batch in enumerate(val_dataloader):
                batch = tuple(b.to(self.device, dtype=torch.float) for b in batch)
                *valid_coeffs, target = batch
                # data = data[..., :self.args.input_dim]
                label = target[..., :self.args.output_dim]
                output = self.model(self.times, valid_coeffs)
                # if self.args.real_value:
                #     label = self.scaler.inverse_transform(label)
                loss = self.loss(output.cuda(), label)
                #a whole batch of Metr_LA is filtered
                if not torch.isnan(loss):
                    total_val_loss += loss.item()
        val_loss = total_val_loss / len(val_dataloader)
        self.logger.info('**********Val Epoch {}: average Loss: {:.6f}'.format(epoch, val_loss))
        if self.args.tensorboard:
            self.w.add_scalar(f'valid/loss', val_loss, epoch)
        return val_loss

    def train_epoch(self, epoch):
        self.model.train()
        total_loss = 0
        # for batch_idx, (data, target) in enumerate(self.train_loader):
        # for batch_idx, (data, target) in enumerate(self.train_loader):
        for batch_idx, batch in enumerate(self.train_loader):
            batch = tuple(b.to(self.device, dtype=torch.float) for b in batch)
            *train_coeffs, target = batch

            # data = data[..., :self.args.input_dim]
            label = target[..., :self.args.output_dim]  # (..., 1)
            self.optimizer.zero_grad()

            # #teacher_forcing for RNN encoder-decoder model
            # #if teacher_forcing_ratio = 1: use label as input in the decoder for all steps
            # if self.args.teacher_forcing:
            #     global_step = (epoch - 1) * self.train_per_epoch + batch_idx
            #     teacher_forcing_ratio = self._compute_sampling_threshold(global_step, self.args.tf_decay_steps)
            # else:
            #     teacher_forcing_ratio = 1.
            #data and target shape: B, T, N, F; output shape: B, T, N, F
            output = self.model(self.times, train_coeffs)
            # output = self.model(train_coeffs, target, teacher_forcing_ratio=teacher_forcing_ratio)
            # if self.args.real_value:
            #     label = self.scaler.inverse_transform(label)
            loss = self.loss(output.cuda(), label)
            
            # loss = _add_weight_regularisation(loss, self.vector_field_g) #TODO: regularization
            # loss = _add_weight_regularisation(loss, self.vector_field_f) #TODO: regularization
            loss.backward()

            # add max grad clipping
            if self.args.grad_norm:
                torch.nn.utils.clip_grad_norm_(self.model.parameters(), self.args.max_grad_norm)
            self.optimizer.step()
            total_loss += loss.item()

            #log information
            if batch_idx % self.args.log_step == 0:
                self.logger.info('Train Epoch {}: {}/{} Loss: {:.6f}'.format(
                    epoch, batch_idx, self.train_per_epoch, loss.item()))
        train_epoch_loss = total_loss/self.train_per_epoch
        self.logger.info('**********Train Epoch {}: averaged Loss: {:.6f}'.format(epoch, train_epoch_loss))
        if self.args.tensorboard:
            self.w.add_scalar(f'train/loss', train_epoch_loss, epoch)

        #learning rate decay
        if self.args.lr_decay:
            self.lr_scheduler.step()
        return train_epoch_loss

    def train(self):
        best_model = None
        best_loss = float('inf')
        not_improved_count = 0
        train_loss_list = []
        val_loss_list = []
        start_time = time.time()
        for epoch in range(1, self.args.epochs + 1):
            epoch_time = time.time()
            train_epoch_loss = self.train_epoch(epoch)
            print("yici time------------", time.time()-epoch_time)
            #exit()
            if self.val_loader == None:
                val_dataloader = self.test_loader
            else:
                val_dataloader = self.val_loader
            val_epoch_loss = self.val_epoch(epoch, val_dataloader)

            #print('LR:', self.optimizer.param_groups[0]['lr'])
            train_loss_list.append(train_epoch_loss)
            val_loss_list.append(val_epoch_loss)
            if train_epoch_loss > 1e6:
                self.logger.warning('Gradient explosion detected. Ending...')
                break
            #if self.val_loader == None:
            #val_epoch_loss = train_epoch_loss
            if val_epoch_loss < best_loss:
                best_loss = val_epoch_loss
                not_improved_count = 0
                best_state = True
            else:
                not_improved_count += 1
                best_state = False
            # early stop
            if self.args.early_stop:
                if not_improved_count == self.args.early_stop_patience:
                    self.logger.info("Validation performance didn\'t improve for {} epochs. "
                                    "Training stops.".format(self.args.early_stop_patience))
                    break
            # save the best state
            if best_state == True:
                self.logger.info('*********************************Current best model saved!')
                best_model = copy.deepcopy(self.model.state_dict())
            
            # if epoch%10==0:#test
            #     self.model.load_state_dict(best_model)
            #     #self.val_epoch(self.args.epochs, self.test_loader)
            #     self.test_simple(self.model, self.args, self.test_loader, self.scaler, self.logger, None, self.times)

        training_time = time.time() - start_time
        self.logger.info("Total training time: {:.4f}min, best loss: {:.6f}".format((training_time / 60), best_loss))

        #save the best model to file
        if not self.args.debug:
            torch.save(best_model, self.best_path)
            self.logger.info("Saving current best model to " + self.best_path)

        # if epoch==10:#test
        #     self.model.load_state_dict(best_model)
        #     #self.val_epoch(self.args.epochs, self.test_loader)
        #     self.test(self.model, self.args, self.test_loader, self.scaler, self.logger, None, self.times)
        self.model.load_state_dict(best_model)
        #self.val_epoch(self.args.epochs, self.test_loader)
        self.test(self.model, self.args, self.test_loader, self.mean_, self.std_, self.logger, None, self.times)

    def save_checkpoint(self):
        state = {
            'state_dict': self.model.state_dict(),
            'optimizer': self.optimizer.state_dict(),
            'config': self.args
        }
        torch.save(state, self.best_path)
        self.logger.info("Saving current best model to " + self.best_path)

    @staticmethod
    def test(model, args, data_loader, mean_, std_, logger, path, times):
        if path != None:
            check_point = torch.load(path)
            state_dict = check_point['state_dict']
            args = check_point['config']
            model.load_state_dict(state_dict)
            model.to(args.device)
        model.eval()
        y_pred = []
        y_true = []
        with torch.no_grad():
            for batch_idx, batch in enumerate(data_loader):
                batch = tuple(b.to(args.device, dtype=torch.float) for b in batch)
                *test_coeffs, target = batch
                label = target[..., :args.output_dim]
                output = model(times.to(args.device, dtype=torch.float), test_coeffs)
                label = label.permute(0, 2, 1, 3)
                output = output.permute(0, 2, 1, 3)

                y_true.append(label.squeeze(-1).cpu().numpy())
                y_pred.append(output.squeeze(-1).cpu().numpy())
                # y_true = scaler.inverse_transform(torch.cat(y_true, dim=0))
                # if args.real_value:
                #     y_pred = torch.cat(y_pred, dim=0)
                # else:
                #     y_pred = torch.cat(y_pred, dim=0)
                # y_pred = scaler.inverse_transform(torch.cat(y_pred, dim=0))
                # np.save(args.log_dir+'/{}_true.npy'.format(args.dataset), y_true.cpu().numpy())
                # np.save(args.log_dir+'/{}_pred.npy'.format(args.dataset), y_pred.cpu().numpy())

            target = np.concatenate(y_true, 0)

            prediction = np.concatenate(y_pred, 0)  # (batch, T', 1)
            print("target:", target.shape)
            print("prediction:", prediction.shape)

            NODE_id = 9

            pre = []
            lab = []

            for i in range(prediction.shape[0]):
                if i % 11 == 0 or i == 0:
                    pre.append(prediction[i, NODE_id, :-1])
                    lab.append(target[i, NODE_id, :-1])
            pre = np.concatenate(pre, 0)  # (batch', 1)
            lab = np.concatenate(lab, 0)  # (batch', 1)
            workbook = xlwt.Workbook(encoding='utf-8')
            worksheet = workbook.add_sheet('My Worksheet')
            worksheet.write(0, 0, "target")
            worksheet.write(0, 1, "prediction")
            print("pre:{pre},  lab:{lab}".format(pre=pre.shape, lab=lab.shape))
            for j in range(pre.shape[0]):
                worksheet.write(j + 1, 0, label=str(lab[j]))
                worksheet.write(j + 1, 1, label=str(pre[j]))

            # 保存
            # workbook.save('PeMS04_single_head.xls')
            workbook.save('my_Pems08_node_9.xls')

            NODE_id = 139

            pre = []
            lab = []

            for i in range(prediction.shape[0]):
                if i % 11 == 0 or i == 0:
                    pre.append(prediction[i, NODE_id, :-1])
                    lab.append(target[i, NODE_id, :-1])
            pre = np.concatenate(pre, 0)  # (batch', 1)
            lab = np.concatenate(lab, 0)  # (batch', 1)
            workbook = xlwt.Workbook(encoding='utf-8')
            worksheet = workbook.add_sheet('My Worksheet')
            worksheet.write(0, 0, "target")
            worksheet.write(0, 1, "prediction")
            print("pre:{pre},  lab:{lab}".format(pre=pre.shape, lab=lab.shape))
            for j in range(pre.shape[0]):
                worksheet.write(j + 1, 0, label=str(lab[j]))
                worksheet.write(j + 1, 1, label=str(pre[j]))

            # 保存
            # workbook.save('PeMS04_single_head.xls')
            workbook.save('my_Pems08_node_139.xls')

            excel_list = []
            prediction_length = prediction.shape[2]

            for i in range(prediction_length):
                assert target.shape[0] == prediction.shape[0]
                mae = mean_absolute_error(target[:, :, i], prediction[:, :, i])
                rmse = mean_squared_error(target[:, :, i], prediction[:, :, i]) ** 0.5
                mape = masked_mape_np(target[:, :, i], prediction[:, :, i], 0)
                print('MAE: %.2f' % (mae))
                print('RMSE: %.2f' % (rmse))
                print('MAPE: %.2f' % (mape))
                excel_list.extend([mae, rmse, mape])

            # print overall results

            mae = mean_absolute_error(target.reshape(-1, 1), prediction.reshape(-1, 1))
            rmse = mean_squared_error(target.reshape(-1, 1), prediction.reshape(-1, 1)) ** 0.5
            mape = masked_mape_np(target.reshape(-1, 1), prediction.reshape(-1, 1), 0)
            print('all MAE: %.2f' % (mae))
            print('all RMSE: %.2f' % (rmse))
            print('all MAPE: %.2f' % (mape))
            excel_list.extend([mae, rmse, mape])
            print(excel_list)

    @staticmethod
    def test_simple(model, args, data_loader, scaler, logger, path, times):
        if path != None:
            check_point = torch.load(path)
            state_dict = check_point['state_dict']
            args = check_point['config']
            model.load_state_dict(state_dict)
            model.to(args.device)
        model.eval()
        y_pred = []
        y_true = []
        with torch.no_grad():
            for batch_idx, batch in enumerate(data_loader):
            # for batch_idx, (data, target) in enumerate(data_loader):
                batch = tuple(b.to(args.device, dtype=torch.float) for b in batch)
                *test_coeffs, target = batch
                # data = data[..., :args.input_dim]
                label = target[..., :args.output_dim]
                output = model(times.to(args.device, dtype=torch.float), test_coeffs)
                y_true.append(label)
                y_pred.append(output)
        y_true = scaler.inverse_transform(torch.cat(y_true, dim=0))
        if args.real_value:
            y_pred = torch.cat(y_pred, dim=0)
        else:
            y_pred = scaler.inverse_transform(torch.cat(y_pred, dim=0))

        for t in range(y_true.shape[1]):
            mae, rmse, mape, _, _ = All_Metrics(y_pred[:, t, ...], y_true[:, t, ...],
                                                args.mae_thresh, args.mape_thresh)
        mae, rmse, mape, _, _ = All_Metrics(y_pred, y_true, args.mae_thresh, args.mape_thresh)
        logger.info("Average Horizon, MAE: {:.2f}, RMSE: {:.2f}, MAPE: {:.4f}%".format(
                    mae, rmse, mape*100))

    @staticmethod
    def _compute_sampling_threshold(global_step, k):
        """
        Computes the sampling probability for scheduled sampling using inverse sigmoid.
        :param global_step:
        :param k:
        :return:
        """
        return k / (k + math.exp(global_step / k))

def _add_weight_regularisation(total_loss, regularise_parameters, scaling=0.03):
    for parameter in regularise_parameters.parameters():
            if parameter.requires_grad:
                total_loss = total_loss + scaling * parameter.norm()
    return total_loss