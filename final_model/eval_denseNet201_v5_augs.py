import torch
import torch.nn.functional as F
import torchvision
import argparse
import numpy as np
import torch.nn as nn
import torch.optim as optim
import data_loader_triplet_v2 as data_loader
from torch.optim.lr_scheduler import StepLR
import matplotlib.pyplot as plt
from matplotlib import cm
import os
import json
from sklearn.manifold import TSNE


def initialize_model(use_pretrained=True, l1Units=512, l2Units=128):
    model = torch.hub.load('pytorch/vision:v0.9.0', 'densenet201', pretrained=use_pretrained)
    for param in model.parameters():
        param.requires_grad = False  # because these layers are pretrained
    # change the final layer to be a bottle neck of two layers
    extracted_features_size = model.classifier.in_features
    model.classifier = nn.Sequential(
        nn.Linear(extracted_features_size, l1Units),
        nn.BatchNorm1d(l1Units),
        nn.ReLU(),
        nn.Linear(l1Units, l2Units)
    )
    return model


def train(args, model, device, train_loader, optimizer, epoch):
    '''
    This is your training function. When you call this function, the model is
    trained for 1 epoch.
    '''
    model.train()  # Set the model to training mode
    for batch_idx, batch in enumerate(train_loader):
        anchor_positive_negative_imgs, anchor_positive_negative_anns = batch
        anchor_img, positive_img, negative_img = anchor_positive_negative_imgs
        anchor_img, positive_img, negative_img = anchor_img.to(device), positive_img.to(device), negative_img.to(device)
        optimizer.zero_grad()  # Clear the gradient
        anchor_emb = model(anchor_img)
        positive_emb = model(positive_img)
        negative_emb = model(negative_img)
        loss = F.triplet_margin_loss(anchor_emb, positive_emb, negative_emb, margin=1.0, p=2)  # sum up batch loss
        loss.backward()  # Gradient computation
        optimizer.step()  # Perform a single optimization step
        if batch_idx % args.batch_log_interval == 0:
            print('Train Epoch: {} [{}/{} ({:.0f}%)]\tLoss: {:.6f}'.format(
                epoch, batch_idx * len(anchor_img), len(train_loader.sampler),
                       100. * batch_idx / len(train_loader), loss.item()))


def test(model, device, test_loader, dataName):
    model.eval()  # Set the model to inference mode
    test_loss = 0
    correct = 0  # number of times it gets the distances correct
    test_num = 0
    with torch.no_grad():  # For the inference step, gradient is not computed
        for batch_idx, batch in enumerate(test_loader):
            anchor_positive_negative_imgs, anchor_positive_negative_anns = batch
            anchor_img, positive_img, negative_img = anchor_positive_negative_imgs
            anchor_img, positive_img, negative_img = anchor_img.to(device), positive_img.to(device), negative_img.to(
                device)
            anchor_emb = model(anchor_img)
            positive_emb = model(positive_img)
            negative_emb = model(negative_img)
            # function that takes output and turns into anchor, positive, negative
            test_loss += F.triplet_margin_loss(anchor_emb, positive_emb, negative_emb, margin=1.0,
                                               p=2)  # sum up batch loss

            predict_match = torch.linalg.norm(anchor_emb - positive_emb, dim=-1) < torch.linalg.norm(
                anchor_emb - negative_emb, dim=-1)

            correct += predict_match.sum()
            test_num += len(predict_match)

    test_loss /= test_num

    print('\n' + dataName + ' tested: Average loss: {:.4f}, Accuracy: {}/{} ({:.0f}%)\n'.format(
        test_loss, correct, test_num,
        100. * correct / test_num))

    return test_loss  # , correct, test_num


def main():
    # Training settings
    # Use the command line to modify the default settings
    parser = argparse.ArgumentParser(description='TripNet: a network for ReID')
    parser.add_argument('--name', default='model',
                        help="what you want to name this model save file")
    parser.add_argument('--epochs', type=int, default=14, metavar='N',
                        help='number of epochs to train (default: 14)')
    parser.add_argument('--lr', type=float, default=1.0, metavar='LR',
                        help='learning rate (default: 0.001)')
    parser.add_argument('--step', type=int, default=1, metavar='N',
                        help='number of epochs between learning rate reductions (default: 1)')
    parser.add_argument('--gamma', type=float, default=0.7, metavar='M',
                        help='Learning rate step gamma (default: 0.7)')
    parser.add_argument('--weight-decay', type=float, default=0.02, metavar='M',
                        help='Learning rate step gamma (default: 0.02)')
    parser.add_argument('--seed', type=int, default=1, metavar='S',
                        help='random seed (default: 1)')
    parser.add_argument('--no-cuda', action='store_true', default=False,
                        help='disables CUDA training')
    parser.add_argument('--load-model', type=str,
                        help='model file path or model name for plotting fract comparison')
    parser.add_argument('--save-model', type=bool, default=True,
                        help='For Saving the current Model')
    parser.add_argument('--batch-size', type=int, default=64,
                        help='Training batch size')
    # Data, model, and output directories
    parser.add_argument('--data-folder',
                        # For AWS, get path from folder
                        default=os.environ.get('SM_CHANNEL_DATA'),
                        help='folder containing data images')
    parser.add_argument('--train-json',
                        # For AWS, get path from folder
                        default=os.path.join(
                            os.environ.get('SM_CHANNEL_ANNOTATIONS', '.'),
                            'customSplit_train.json'
                        ),
                        help='JSON with COCO-format annotations for training dataset')
    parser.add_argument('--val-json',
                        # For AWS, get path from folder
                        default=os.path.join(
                            os.environ.get('SM_CHANNEL_ANNOTATIONS', '.'),
                            'customSplit_val.json'
                        ),
                        help='JSON with COCO-format annotations for validation dataset')
    parser.add_argument('--model-dir', type=str, default=os.environ.get('SM_MODEL_DIR', '.'))
    parser.add_argument('--batch-log-interval', type=int, default=10,
                        help='Number of batches to run each epoch before logging metrics.')
    parser.add_argument('--num-train-triplets', type=int, default=10 * 1000,
                        help='Number of triplets to generate for each training epoch.')
    parser.add_argument('--use-seg', type=bool, default=False,
                        help='For using semantic segmentations')
    parser.add_argument('--use-bbox', type=bool, default=False,
                        help='For cropping to bounding box')
    parser.add_argument('--evaluate', action='store_true', default=False,
                        help='For evaluating model performance after training')
    parser.add_argument('--image-size', type=int, default=224,
                        help='Input to CNN will be size (image_size, image_size, 3)')
    parser.add_argument('--apply-augmentation', action='store_true', default=False,
                        help='Applies image augmentations')
    args = parser.parse_args()
    use_cuda = not args.no_cuda and torch.cuda.is_available()
    use_seg = args.use_seg
    use_bbox = args.use_bbox
    use_aug = args.apply_augmentation
    print('use seg?', use_seg)
    print('use bbox?', use_bbox)
    print('use aug?', use_aug)
    np.random.seed(2021)  # to ensure you always get the same train/test split
    torch.manual_seed(args.seed)
    device = torch.device("cuda" if use_cuda else "cpu")
    kwargs = {'num_workers': 1, 'pin_memory': True} if use_cuda else {}

    # Define transforms
    # torchvision pretrained models tend to use 224x224 images
    downsample = torchvision.transforms.Compose([
        torchvision.transforms.Resize(args.image_size),
        # Assume that zebra is centered
        torchvision.transforms.CenterCrop(args.image_size),
    ])

    augment1 = torchvision.transforms.RandomChoice(
        [torchvision.transforms.RandomAffine(degrees=10, translate=(0.05, 0.05), shear=10),
         torchvision.transforms.ColorJitter(brightness=(1, 1), contrast=(1, 1), saturation=(1, 1), hue=(-0.1, 0.1)),
         torchvision.transforms.RandomPerspective(distortion_scale=0.20, p=1),
         torchvision.transforms.GaussianBlur(5, sigma=(0.1, 1.5))]
    )

    # Pretrained torchvision models need specific normalization;
    # see https://pytorch.org/vision/stable/models.html
    normalize = torchvision.transforms.Normalize(mean=[0.485, 0.456, 0.406],
                                                 std=[0.229, 0.224, 0.225])

    augment2 = torchvision.transforms.RandomErasing(p=0.5, scale=(0.02, 0.10), ratio=(0.3, 3.3), value=0, inplace=False)

    transforms_aug = torchvision.transforms.Compose([
        downsample,
        augment1,
        torchvision.transforms.ToTensor(),
        normalize,
        augment2,
    ])

    transforms = torchvision.transforms.Compose([
        downsample,
        torchvision.transforms.ToTensor(),
        normalize,
    ])

    if args.evaluate:
        print('EVALUATING MODEL')
        # generate some plots, don't actually train the model
        modelName = args.name + '_model.pt'
        model = initialize_model()
        model = model.to(device)
        model.load_state_dict(torch.load(modelName))
        model.eval()

        # load the underlying annotations file for the
        BOX_ANNOTATION_FILE = '../../Data/gzgc.coco/masks/instances_train2020_maskrcnn.json'
        with open(BOX_ANNOTATION_FILE) as f:
            annData = json.load(f)
        f.close()
        annData = annData['annotations']  # just the annotations
        if use_aug:
            val_loader = data_loader.get_loader(
                args.data_folder,
                args.val_json,
                transforms_aug,
                batch_size=args.batch_size,
                shuffle=True,
                num_triplets=args.num_train_triplets,
                apply_mask=use_seg,
                apply_mask_bbox=use_bbox,
            )
        else:
            val_loader = data_loader.get_loader(
                args.data_folder,
                args.val_json,
                transforms,
                batch_size=args.batch_size,
                shuffle=True,
                num_triplets=int(args.num_train_triplets),
                apply_mask=use_seg,
                apply_mask_bbox=use_bbox,
            )
        print('computing errors')
        # plot 6 errors on the validation set and count total number of errors
        wrong_trip = []
        wrong_posDis = []
        wrong_negDis = []
        totError = 0.0
        totCount = 0
        loggedAnns = []
        zebIDs = []
        allEmbeds = []
        allIms = []
        corCount = 0.0
        cor_trip = []
        cor_posDis = []
        cor_negDis = []
        with torch.no_grad():  # For the inference step, gradient is not computed
            for (img1, img2, img3), (ann1, ann2, ann3) in val_loader:
                img1Dev, img2Dev, img3Dev = img1.to(device), img2.to(device), img3.to(device)
                anchor_emb = model(img1Dev)  # just use these
                positive_emb = model(img2Dev)
                negative_emb = model(img3Dev)
                # find the errors
                for i, anc in enumerate(anchor_emb):
                    if np.linalg.norm(anc.cpu() - positive_emb.cpu()[i]) >= np.linalg.norm(
                            anc.cpu() - negative_emb.cpu()[i]):
                        totError += 1.0
                        if totError <= 6.0:
                            wrong_trip.append([img1[i], img2[i], img3[i]])  # anchor, positive, negative
                            wrong_posDis.append(np.linalg.norm(anc.cpu() - positive_emb.cpu()[i]))
                            wrong_negDis.append(np.linalg.norm(anc.cpu() - negative_emb.cpu()[i]))
                    elif corCount <= 6.0:  # just grab the first 6 correct triplets
                        corCount += 1
                        cor_trip.append([img1[i], img2[i], img3[i]])  # anchor, positive, negative
                        cor_posDis.append(np.linalg.norm(anc.cpu() - positive_emb.cpu()[i]))
                        cor_negDis.append(np.linalg.norm(anc.cpu() - negative_emb.cpu()[i]))
                    totCount += 1
                for i, anc in enumerate(anchor_emb):
                    annID = float(ann1[i].numpy())
                    if annID not in loggedAnns:
                        zebIDs.append(val_loader.dataset.annotations[annID]['name'])
                        allEmbeds.append(anc.cpu().numpy().copy())
                        allIms.append(img1[i].permute(1, 2, 0).numpy().copy())
                        loggedAnns.append(annID)

        print('************')
        print('total error: ' + str(totError) + '/' + str(totCount) + ' = ' + str(totError / totCount) + '%')

        zebNames = np.unique(zebIDs)
        print('num distinct anchor annotations: ' + str(len(zebIDs)))
        print('num distinct anchor individuals: ' + str(len(zebNames)))

        # now visualize with tSNE all the zebra embeddings - each a different color
        feat_embedded = TSNE(n_components=2, n_iter=500).fit_transform(np.array(allEmbeds))

        # plot, color coded so one zebra name has one color
        # plot scatter with limited number of zebras
        f = plt.figure(figsize=(6,5))
        ax = plt.subplot()
        cmap = cm.get_cmap('gist_rainbow', 15)
        cmap = cmap(range(15))
        cI = 0
        for iter, name in enumerate(zebNames):
            if cI <15: # don't want to plot everything
                # pull all the points with that target
                inds = [i for i, e in enumerate(zebIDs) if e == name]
                iter_feat = np.squeeze(feat_embedded[inds, :])
                if type(inds) == int:
                    ax.scatter(iter_feat[0], iter_feat[1], c=np.array([cmap[iter,:]]), s=35, label=name,
                               alpha=0.8, edgecolors='none')
                elif len(inds) > 1:
                    ax.scatter(iter_feat[:,0], iter_feat[:,1], c=np.array([cmap[iter,:]]), s=35, label=name,
                               alpha=0.8, edgecolors='none')
                cI += 1
        ax.set_title('tSNE scatter of embedding')
        #plt.legend()
        plt.show()

        # plot scatter with all zebras
        f = plt.figure(figsize=(6,5))
        ax = plt.subplot()
        cmap = cm.get_cmap('gist_rainbow', len(zebNames))
        cmap = cmap(range(len(zebNames)))
        for iter, name in enumerate(zebNames):
            # pull all the points with that target
            inds = [i for i, e in enumerate(zebIDs) if e == name]
            iter_feat = np.squeeze(feat_embedded[inds, :])
            if type(inds) == int:
                ax.scatter(iter_feat[0], iter_feat[1], c=np.array([cmap[iter,:]]), s=35, label=name,
                           alpha=0.8, edgecolors='none')
            elif len(inds) > 1:
                ax.scatter(iter_feat[:,0], iter_feat[:,1], c=np.array([cmap[iter,:]]), s=35, label=name,
                           alpha=0.8, edgecolors='none')
        ax.set_title('tSNE scatter of embedding')
        #plt.legend()
        plt.show()

        # visualize 4 annotations with each the four annotations closest
        sampleImInds = np.random.choice(len(loggedAnns), size=40, replace=False)
        # sampleImInds = sampleImInds[20:] # uncomment this to get the chosen ranks in final presentation
        f = plt.figure(figsize=(6, 5))
        for i in range(4):
            matchingInd = []
            dist = np.zeros((len(allEmbeds)))
            # get the four closest embeddings
            anc = allEmbeds[sampleImInds[i]]
            for j, emb in enumerate(allEmbeds):
                if j != sampleImInds[i]:
                    dist[j] = np.linalg.norm(anc - emb)
                    if zebIDs[j] == zebIDs[sampleImInds[i]]:
                        matchingInd.append(j)
            dist[sampleImInds[i]] = np.max(dist) * 2.0  # just insuring we don't pick the same image again
            bestInds = np.argsort(dist)[:5]  # sort smallest to largest
            absMax = np.max(np.max(np.abs(allIms[sampleImInds[i]])))

            ax = plt.subplot(4, 5, i * 5 + 1)
            plt.imshow((allIms[sampleImInds[i]] + absMax) / (2.0 * absMax))
            ax.set_title('anchor ' + zebIDs[sampleImInds[i]])
            plt.xticks([])
            plt.yticks([])

            for j in range(4):
                absMax = np.max(np.max(np.abs(allIms[bestInds[j]])))
                ax = plt.subplot(4, 5, i * 5 + 2 + j)
                plt.imshow((allIms[bestInds[j]] + absMax) / (2.0 * absMax))
                ax.set_title('rank ' + str(j + 1) + ': ' + zebIDs[bestInds[j]])
                plt.xticks([])
                plt.yticks([])
        plt.tight_layout()
        plt.show()

        # calculate anchor ranking error - top 1 and top 5
        rankErr = 0
        top5rankErr = 0
        totRankCount = 0
        for i, anc in enumerate(allEmbeds):
            # for every annotation, find the closest match and see if it does match
            dist = np.zeros((len(allEmbeds)))
            # get the four closest embeddings
            for j, emb in enumerate(allEmbeds):
                if j != i:
                    dist[j] = (np.linalg.norm(anc - emb))
            dist[i] = np.max(dist) * 2.0  # just insuring we don't pick the same image again
            # top 1 error
            matchInd = np.argsort(dist)[0]  # sort smallest to largest, get smallest
            if zebIDs[matchInd] != zebIDs[i]:  # if the names are not the same
                rankErr += 1
            # top 5 error
            matchInd = np.argsort(dist)[:5]  # sort smallest to largest, get top 5 smallest
            isMatch = False
            for ind in matchInd:
                if zebIDs[ind] == zebIDs[i]:  # if the names are the same
                    isMatch = True
            if not isMatch:  # if no match in top 5
                top5rankErr += 1
            totRankCount += 1
        print('************')
        print('top 1 ranking error: ' + str(rankErr) + '/' + str(totRankCount) + ' = ' + str(rankErr / totRankCount))
        print('top 5 ranking error: ' + str(top5rankErr) + '/' + str(totRankCount) + ' = ' + str(
            top5rankErr / totRankCount))

        # visualize 4 correct ones
        f = plt.figure(figsize=(6, 5))
        for i in range(4):  # plot 5 errors
            triplet = cor_trip[i]
            # sample = sample.byte()
            # plot
            anc = triplet[0].permute(1, 2, 0)

            # anc = transforms.functional.to_pil_image(anc.byte())
            anc = np.asarray(anc)
            pos = triplet[1].permute(1, 2, 0)
            # pos = transforms.functional.to_pil_image(pos.byte())
            pos = np.asarray(pos)
            neg = triplet[2].permute(1, 2, 0)
            # neg = transforms.functional.to_pil_image(neg.byte())
            neg = np.asarray(neg)
            ax = plt.subplot(4, 3, i * 3 + 1)
            absMax = np.max(np.max(np.abs(anc)))
            plt.imshow((anc + absMax) / (2.0 * absMax))
            ax.set_title('anchor')
            plt.xticks([])
            plt.yticks([])

            ax = plt.subplot(4, 3, i * 3 + 2)
            absMax = np.max(np.max(np.abs(pos)))
            plt.imshow((pos + absMax) / (2.0 * absMax))
            ax.set_title('pos dist=' + str(cor_posDis[i]))
            plt.xticks([])
            plt.yticks([])

            ax = plt.subplot(4, 3, i * 3 + 3)
            absMax = np.max(np.max(np.abs(neg)))
            plt.imshow((neg + absMax) / (2.0 * absMax))
            ax.set_title('neg dist=' + str(cor_negDis[i]))
            plt.xticks([])
            plt.yticks([])
        plt.tight_layout()
        plt.show()

        # visualize 4 errors
        f = plt.figure(figsize=(6, 5))
        for i in range(4):  # plot 5 errors
            triplet = wrong_trip[i]
            # sample = sample.byte()
            # plot
            anc = triplet[0].permute(1, 2, 0)

            # anc = transforms.functional.to_pil_image(anc.byte())
            anc = np.asarray(anc)
            pos = triplet[1].permute(1, 2, 0)
            # pos = transforms.functional.to_pil_image(pos.byte())
            pos = np.asarray(pos)
            neg = triplet[2].permute(1, 2, 0)
            # neg = transforms.functional.to_pil_image(neg.byte())
            neg = np.asarray(neg)
            ax = plt.subplot(4, 3, i * 3 + 1)
            absMax = np.max(np.max(np.abs(anc)))
            plt.imshow((anc + absMax) / (2.0 * absMax))
            ax.set_title('anchor')
            plt.xticks([])
            plt.yticks([])

            ax = plt.subplot(4, 3, i * 3 + 2)
            absMax = np.max(np.max(np.abs(pos)))
            plt.imshow((pos + absMax) / (2.0 * absMax))
            ax.set_title('pos dist=' + str(wrong_posDis[i]))
            plt.xticks([])
            plt.yticks([])

            ax = plt.subplot(4, 3, i * 3 + 3)
            absMax = np.max(np.max(np.abs(neg)))
            plt.imshow((neg + absMax) / (2.0 * absMax))
            ax.set_title('neg dist=' + str(wrong_negDis[i]))
            plt.xticks([])
            plt.yticks([])
        plt.tight_layout()
        plt.show()

        return

    # Initialize dataset loaders
    if use_aug:
        train_loader = data_loader.get_loader(
            args.data_folder,
            args.train_json,
            transforms_aug,
            batch_size=args.batch_size,
            shuffle=True,
            num_triplets=args.num_train_triplets,
            apply_mask=use_seg,
            apply_mask_bbox=use_bbox,
        )
    else:
        train_loader = data_loader.get_loader(
            args.data_folder,
            args.train_json,
            transforms,
            batch_size=args.batch_size,
            shuffle=True,
            num_triplets=args.num_train_triplets,
            apply_mask=use_seg,
            apply_mask_bbox=use_bbox,
        )
    val_loader = data_loader.get_loader(
        args.data_folder,
        args.val_json,
        transforms,
        batch_size=args.batch_size,
        shuffle=True,
        num_triplets=int(0.15 * args.num_train_triplets),
        apply_mask=use_seg,
        apply_mask_bbox=use_bbox,
    )

    # object recognition, pretrained on imagenet
    # https://pytorch.org/hub/pytorch_vision_densenet/
    model = initialize_model()
    # print(model)
    model = model.to(device)
    # Try different optimzers here [Adam, SGD, RMSprop]
    optimizer = optim.Adam(model.parameters(), lr=args.lr, weight_decay=args.weight_decay)

    # Set your learning rate scheduler
    scheduler = StepLR(optimizer, step_size=args.step, gamma=args.gamma)

    # Training loop
    trainLoss = []
    valLoss = []
    for epoch in range(1, args.epochs + 1):
        train(args, model, device, train_loader, optimizer, epoch)  # None placeholder for triplet loss argument
        trloss = test(model, device, train_loader, "train data")  # training loss
        vloss = test(model, device, val_loader, "val data")  # validation loss
        trainLoss.append(trloss)
        valLoss.append(vloss)
        scheduler.step()  # learning rate scheduler

        if args.save_model:
            torch.save(model.state_dict(), os.path.join(args.model_dir, args.name + "_model.pt"))

    # plot training and validation loss by epoch
    f = plt.figure(figsize=(6, 5))
    ax = plt.subplot()
    plt.plot(range(1, args.epochs + 1), trainLoss, label="training loss")
    plt.plot(range(1, args.epochs + 1), valLoss, label="validation loss")
    ax.set_title('loss over epochs')
    # plt.xlim([0, 1.1])
    # plt.ylim([0, 1.1])
    plt.xlabel('epoch')
    plt.ylabel('loss')
    plt.legend()
    plt.show()


# feed into the network triplets of zebras, with segmented out backgrounds

if __name__ == '__main__':
    main()
