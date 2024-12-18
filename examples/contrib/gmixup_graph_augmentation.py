"""This is an example of using the G-Mixup graph generator for graph
classification data augmentation
"""
import argparse

import torch
import torch.nn as nn
import torch.nn.functional as F

import torch_geometric
import torch_geometric.nn as pyg_nn
import torch_geometric.utils
from torch_geometric.data import Dataset, InMemoryDataset
from torch_geometric.loader import DataLoader
from torch_geometric.datasets import TUDataset
from torch_geometric.contrib.augmentation import GMixup


class GCN(nn.Module):
    def __init__(self, input_dim, output_dim, hidden_dim=64):
        super(GCN, self).__init__()
        self.gcn = pyg_nn.GCN(
            in_channels=input_dim,
            hidden_channels=hidden_dim,
            num_layers=4,
            out_channels=output_dim,
            act='relu',
        )

    def forward(self, x, edge_index, batch):
        x = self.gcn(x, edge_index)
        return pyg_nn.global_mean_pool(x, batch)
    
    def predict(self, x, edge_index, batch):
        x = self.forward(x, edge_index, batch)
        return torch.argmax(x, dim=1)


def create_node_features(dataset: Dataset):
    num_classes = dataset.num_classes
    dataset = list(dataset)
    # Get node degrees: list of tensors of shape (num_nodes,)
    all_degrees = []
    for graph in dataset:
        all_degrees.append(
            torch_geometric.utils.degree(graph.edge_index[0], dtype=torch.long)
        )
        graph.num_nodes = int(torch.max(graph.edge_index)) + 1
        # Make labels one-hot
        graph.y = F.one_hot(graph.y.long(), num_classes=num_classes).float()
    max_degree = max(d.max().item() for d in all_degrees)
    # If sparse enough, use one-hot
    if max_degree < 2000:
        for graph, degrees in zip(dataset, all_degrees):
            graph.x = F.one_hot(degrees, num_classes=max_degree+1).float()
    # Else use degree z-score
    else:
        std, mean = torch.std_mean(torch.cat(all_degrees).float())
        std, mean = std.item(), mean.item()
        for graph, degrees in zip(dataset, all_degrees):
            graph.x = ((degrees - mean) / std).view(-1, 1)
    return dataset


if __name__ == '__main__':

    parser = argparse.ArgumentParser()
    parser.add_argument('--dataset', type=str, default='IMDB-BINARY')
    parser.add_argument('--dataset-path', type=str, default='./datasets/')
    parser.add_argument('--seed', type=int, default=42)
    # Training
    parser.add_argument('--batch-size', type=int, default=128)
    parser.add_argument('--epochs', type=int, default=100)
    parser.add_argument('--lr', type=float, default=0.01)
    # GMixup
    parser.add_argument('--vanilla', dest='use_mixup', action='store_false')
    parser.add_argument('--aug-ratio', type=float, default=0.5)
    parser.add_argument('--interpolation-range',
                        nargs=2, type=float, default=(0.1,0.2))
    args = parser.parse_args()

    torch.manual_seed(args.seed)

    ds = TUDataset(args.dataset_path, args.dataset)
    ds.print_summary()
    ds = create_node_features(ds)
    ntrain = int(0.8 * len(ds))
    shuffled_indices = torch.randperm(len(ds))
    train = [ds[i] for i in shuffled_indices[:ntrain]]
    test = [ds[i] for i in shuffled_indices[ntrain:]]
    print(f'Train: {len(train)} graphs | Test: {len(test)} graphs')


    if args.use_mixup:
        
        gmixup = GMixup(train)
        synthetic = gmixup.generate(
            num_samples=int(len(train) * args.aug_ratio),
            interpolation_range=args.interpolation_range,
        )
        print(f'Generated {len(synthetic)} synthetic graphs')
        dataloader = DataLoader(
            train + synthetic,
            batch_size=args.batch_size,
            shuffle=True
        )

    else:
        dataloader = DataLoader(
            train,
            batch_size=args.batch_size,
            shuffle=True
        )

    device = torch.device('cuda' if torch.cuda.is_available() else 'cpu')
    model = GCN(ds.num_features, ds.num_classes).to(device).train()
    optimizer = torch.optim.Adam(model.parameters(), lr=args.lr)
    scheduler = torch.optim.lr_scheduler.StepLR(
        optimizer, step_size=100, gamma=0.5)

    # Training loop
    for epoch in range(args.epochs):
        total_loss, total_samples = 0, 0
        for batch in dataloader:
            batch = batch.to(device)
            out = model.forward(batch.x, batch.edge_index, batch.batch)
            # Compute loss
            loss = F.cross_entropy(out, batch.y)
            total_loss += len(batch) * loss.item()
            total_samples += len(batch)
            # Backward pass and optimization step
            optimizer.zero_grad()
            loss.backward()
            optimizer.step()
        scheduler.step()
        
        avg_loss = total_loss / total_samples
        print(f'Epoch {epoch+1}/{args.epochs}, Loss: {avg_loss:.4f}')
    
    # Evaluate
    model.eval()
    total_correct, total_samples = 0, 0
    for batch in DataLoader(test, args.batch_size):
        batch = batch.to(device)
        out = model.predict(batch.x, batch.edge_index, batch.batch)
        # Convert one-hot to class indices
        labels = torch.argmax(batch.y, dim=1)
        total_correct += (out == labels).sum()
        total_samples += len(batch)
    acc = total_correct / total_samples
    print(f'Test Accuracy: {100*acc:.3f}%')


