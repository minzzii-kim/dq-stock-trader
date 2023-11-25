import os
from datetime import datetime
import torch

from os import path
import numpy as np
import random
from collections import deque


class QNetwork(torch.nn.Module):
    def __init__(self, state_size, action_size):
        super().__init__()
        self.state_size = state_size
        self.action_size = action_size
        self.lp = dict(
            criterion=torch.nn.MSELoss(),
            max_epochs=15, n_features=1,
            hidden_size=50, num_layers=1, dropout=0.8, learning_rate=0.01
        )  # lp: lstm에 들어가는 layer params

        # lstm
        self.lstm = self.lstm_feature()
        self.lstm_out = torch.nn.Linear(
            in_features=self.lp['hidden_size'], out_features=action_size
        )  # lstm 만 학습할 때 쓰는 layer

        # cnn
        self.cnn = self.cnn_feature()
        self.cnn_fc1 = torch.nn.Linear(in_features=3136, out_features=112)  # cnn만 학습할 때 쓰는 layer
        self.cnn_fc2 = torch.nn.Linear(in_features=112, out_features=64)
        self.cnn_fc3 = torch.nn.Linear(in_features=64, out_features=32)
        self.cnn_out = torch.nn.Linear(in_features=32, out_features=action_size)

        # fusion
        self.dropout = torch.nn.Dropout(0.5)
        self.fc1 = torch.nn.Linear(in_features=12086, out_features=256)
        self.fc2 = torch.nn.Linear(in_features=256, out_features=128)
        self.fc3 = torch.nn.Linear(in_features=128, out_features=32)
        self.out = torch.nn.Linear(in_features=32, out_features=action_size)
        self.out_act = torch.nn.Softmax()

    def cnn_feature(self):
        return torch.nn.Sequential(
            torch.nn.Conv2d(in_channels=3, out_channels=8, kernel_size=(7, 7), padding="same"),
            torch.nn.BatchNorm2d(8),
            torch.nn.MaxPool2d(kernel_size=3, stride=2),
            torch.nn.LeakyReLU(),

            torch.nn.Conv2d(in_channels=8, out_channels=16, kernel_size=7, stride=2, padding=3),
            torch.nn.BatchNorm2d(16),
            torch.nn.MaxPool2d(kernel_size=3, stride=2, padding=1),
            torch.nn.LeakyReLU(),
        )

    def lstm_feature(self):
        return torch.nn.LSTM(
            input_size=6,  # 칼럼이 6개, ohlcv, adjclose, ma15
            hidden_size=self.lp['hidden_size'],
            num_layers=self.lp['num_layers'],
            dropout=self.lp['dropout'],
            batch_first=True
        )

    def forward(self, x1, x2):
        # x1: 시계열 정형데이터, x2: 이미지 데이터
        x1, _ = self.lstm(x1)
        x2 = self.cnn(torch.stack([x2]))
        x1 = x1.view(x1.size(0), -1)
        x2 = x2.view(x2.size(0), -1)
        fusion_out = torch.cat((x1, x2), 1)
        fusion_out = self.fc1(fusion_out)
        fusion_out = self.dropout(fusion_out)
        fusion_out = self.fc2(fusion_out)
        fusion_out = self.dropout(fusion_out)
        fusion_out = self.fc3(fusion_out)
        fusion_out = self.dropout(fusion_out)
        fusion_out = self.out(fusion_out)
        fusion_out = self.out_act(fusion_out)
        return fusion_out


class Agent:
    def __init__(self, state_size, is_eval=False, model_name=""):
        self.state_size = state_size  # normalized previous days
        self.action_size = 3  # sit, buy, sell
        self.memory = deque(maxlen=10000)
        self.inventory = []
        self.model_name = model_name
        self.is_eval = is_eval

        self.gamma = 0.95
        self.epsilon = 1.0
        self.epsilon_min = 0.01
        self.epsilon_decay = 0.995
        self.target_update_period = 4

        self.target_network = QNetwork(self.state_size, self.action_size)
        self.online_network = QNetwork(self.state_size, self.action_size)

        if is_eval:
            self.load()
            self.online_network.eval()

        self.optimizer = torch.optim.Adam(self.online_network.parameters(), lr=0.0005)

    def save(self, e, stock_name):
        folder = f"models/{stock_name}"
        if not os.path.exists(folder):
            os.makedirs(folder, exist_ok=True)
        torch.save(self.target_network.state_dict(), f"models/{stock_name}/{stock_name}_model_ep" + str(e) + ".pt")

    def load(self):
        self.online_network.load_state_dict(torch.load("models/" + self.model_name + ".pt"))

    def act(self, num_state, img_state):
        if not self.is_eval and np.random.rand() <= self.epsilon:
            return random.randrange(self.action_size)

        with torch.no_grad():
            num_state = torch.tensor(num_state, dtype=torch.float)
            options = self.online_network(num_state, img_state)

        return torch.argmax(options[0]).item()  # options.shape : (window_size, actoin_size)

    def update_target(self):
        self.target_network.load_state_dict(self.online_network.state_dict())

    def expReplay(self, batch_size):
        loss = 0
        for state, action, reward, state_next, done in random.choices(self.memory, k=batch_size):
            num_state = torch.tensor(state[0], dtype=torch.float)
            img_state = state[1]
            action = torch.tensor(action, dtype=torch.long)
            nxt_num_state = torch.tensor(state_next[0], dtype=torch.float)
            nxt_img_state = state_next[1]
            reward = torch.tensor(reward, dtype=torch.float)
            done = torch.tensor(done, dtype=torch.float)
            # print(f"action : {action}")
            with torch.no_grad():
                # 궁금한점: f.target_network(nxt_num_state, nxt_img_state)의 output이 (0score, 1score, 2score)인데요, max 대신 index 로바꿔야하나요?
                target = reward + (1 - done) * self.gamma * torch.max(self.target_network(nxt_num_state, nxt_img_state))

            loss += (target - self.online_network(num_state, img_state)[0][action]) ** 2

        loss /= batch_size

        self.optimizer.zero_grad()
        loss.backward()
        self.optimizer.step()

        if self.epsilon > self.epsilon_min:
            self.epsilon *= self.epsilon_decay
