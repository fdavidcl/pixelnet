#!/usr/bin/env python

import os
import numpy as np
import scipy.ndimage as ndi

import tensorflow as tf
from tensorflow.keras import backend as K
from tensorflow.keras.utils import to_categorical
# from tensorflow.keras.preprocessing.image import flip_axis

def random_intensity_shift(x, intensity_fraction=0.01):
    min_x, max_x = np.min(x), np.max(x)
    intensity = intensity_fraction * max_x - min_x
    return np.clip(x + np.random.uniform(-intensity, intensity), min_x, max_x)

def augment(I, L, rotation_range, zoom_range, horizontal_flip=False, vertical_flip=False, intensity_shift=0):
    """ Apply random image rotation and zoom with mirror boundary conditions
    crop to original spatial dimensions
    """
    num_images, h, w, c = I.shape
    for idx in range(num_images):
        II, LL = I[idx], L[idx]

        if horizontal_flip:
            if np.random.random() < 0.5:
                II = np.flip(II, 1)
                LL = np.flip(LL, 1)

        if vertical_flip:
            if np.random.random() < 0.5:
                II = np.flip(II, 0)
                LL = np.flip(LL, 0)

        if intensity_shift > 0:
            II = random_intensity_shift(II, intensity_fraction=intensity_shift)

        # apply a random rotation with out cropping back to the original size
        angle =  rotation_range * np.random.random()
        II = ndi.rotate(II, angle, reshape=False, mode='reflect')
        LL = ndi.rotate(LL, angle, reshape=False, mode='reflect', order=0)

        # apply a random zoom
        zfactor = 1 + zoom_range*np.random.random()
        II = ndi.zoom(II, zfactor)
        LL = ndi.zoom(LL, zfactor, order=0)

        # crop out a region of the original image shape
        hh, ww, cc = II.shape
        x = np.random.choice(range(max(1,ww-w)))
        y = np.random.choice(range(max(1,hh-h)))

        # overwrite the input
        I[idx] = II[y:y+h, x:x+w]
        L[idx] = LL[y:y+h,x:x+w]

    return I, L

def random_crop(images, labels, cropsize):
    """ randomly crop an image tensor to (batch, cropsize, cropsize, channels) """
    b, h, w, c = images.shape

    # preallocate output tensors
    I = np.zeros((b, cropsize, cropsize, c), dtype=images.dtype)
    L = np.zeros((b, cropsize, cropsize), dtype=labels.dtype)

    # choose random cropsizeXcropsize windows
    xx = np.random.choice(range(w - cropsize), size=b)
    yy = np.random.choice(range(h - cropsize), size=b)

    # crop input image and labels consistently
    for idx in range(b):
        x, y = xx[idx], yy[idx]
        I[idx] = images[idx,y:y+cropsize,x:x+cropsize,:]
        L[idx] = labels[idx,y:y+cropsize,x:x+cropsize]

    return I, L

def random_crop_generator(images, labels, batchsize=4, cropsize=224, nclasses=4):
    b, h, w, c = images.shape

    while True:
        # preallocate output tensors
        I = np.zeros((b, cropsize, cropsize, c), dtype=images.dtype)
        L = np.zeros((b, cropsize, cropsize), dtype=labels.dtype)

        # choose random cropsizeXcropsize windows
        xx = np.random.choice(range(w - cropsize), size=b)
        yy = np.random.choice(range(h - cropsize), size=b)

        # crop input image and labels consistently
        for idx in range(b):
            x, y = xx[idx], yy[idx]
            I[idx] = images[idx,y:y+cropsize,x:x+cropsize,:]
            L[idx] = labels[idx,y:y+cropsize,x:x+cropsize]

        s = L.shape
        L = to_categorical(L.flat, num_classes=nclasses)
        L = L.reshape((*s, nclasses))
        yield I, L


def random_pixel_samples(images, labels, batchsize=4, npix=2048, cropsize=None, nclasses=4, replace_samples=True, categorical=True,
                         confidence=1.0, horizontal_flip=False, vertical_flip=False, rotation_range=0.0, zoom_range=0.0, intensity_shift=0.0):
    """ generate random samples of pixels in batches of training images """
    n_images = images.shape[0]

    pixel_labels = tf.placeholder(tf.float32, shape=(batchsize, npix))
    while True:
        # choose random batch of images, with replacement
        im_idx = np.random.choice(range(n_images), batchsize, replace=replace_samples)
        sample_images = images[im_idx]
        target_labels = labels[im_idx]

        # jointly apply transformations to input and label images for data augmentation
        if horizontal_flip or vertical_flip or rotation_range or zoom_range:
            sample_images, target_labels = augment(sample_images, target_labels, rotation_range, zoom_range,
                                                   horizontal_flip, vertical_flip, intensity_shift)

        if cropsize is not None:
            sample_images, target_labels = random_crop(sample_images, target_labels, cropsize)

        # sample coordinates should include the batch index for tf.gather_nd
        coords = np.ones((batchsize, npix, 3))
        coords = coords * np.arange(batchsize)[:,np.newaxis,np.newaxis]

        # choose random pixel coordinates
        xx = np.random.randint(sample_images.shape[1]-1, size=(batchsize, npix))
        yy = np.random.randint(sample_images.shape[2]-1, size=(batchsize, npix))
        p = np.dstack((xx, yy))
        coords[:,:,1:] = p / np.array([sample_images.shape[1], sample_images.shape[2]])

        # get sample pixel labels
        bb = coords[...,0].astype(np.int32)
        # ind = coords * np.array([1, sample_images.shape[1], sample_images.shape[2]])
        # ind = ind.astype(np.int32)
        # bb, xx, yy = ind[:,:,0], ind[:,:,1], ind[:,:,2]
        pixel_labels = target_labels[bb,xx,yy]

        if categorical:
            # convert labels to categorical indicators for cross-entropy loss
            s = pixel_labels.shape
            pixel_labels = to_categorical(pixel_labels.flat, num_classes=nclasses)
            if np.any(confidence != 1.0):
                pixel_labels = smooth_labels(pixel_labels, confidence=confidence)
            pixel_labels = pixel_labels.reshape((s[0], s[1], nclasses))

        yield ([sample_images, coords], pixel_labels)

def smooth_labels(labels, confidence=1.0):
    """ Apply label smoothing for classification task (arXiv:1512.00567) """

    nclasses = labels.shape[1]

    if type(confidence) is float:
        epsilon = 1 - confidence
        labels = labels * (1 - epsilon)
        labels += (epsilon / nclasses)

    elif type(confidence) is np.array:
        # confidence is an nclasses by  nclasses array
        # one row for each class
        # column values indicate confidence (rows sum to 1)
        # the identity matrix indicates full confidence
        # ex: 70% confident in the labels for class 1
        #     uniform prior on label error distribution
        # then row 1 is [0.1, 0.7, 0.1, 0.1]
        for cls in range(nclasses):
            labels[np.where(labels[...,cls])] = confidence[cls]
    return labels

def stratified_pixel_samples(images, labels, batchsize=4, npix=2048, cropsize=None, nclasses=4, replace_samples=True, categorical=True,
                             confidence=1.0, horizontal_flip=False, vertical_flip=False, rotation_range=0.0, zoom_range=0.0, intensity_shift=0.0):
    """ generate samples of pixels in batches of training images
    try to balance the class distribution over the minibatch.
    """
    n_images = images.shape[0]

    pixel_labels = tf.placeholder(tf.float32, shape=(batchsize, npix))
    while True:
        # choose random batch of images, with replacement
        im_idx = np.random.choice(range(n_images), batchsize, replace=replace_samples)
        sample_images = images[im_idx]
        target_labels = labels[im_idx]

        # jointly apply transformations to input and label images for data augmentation
        if horizontal_flip or vertical_flip or rotation_range or zoom_range:
            sample_images, target_labels = augment(sample_images, target_labels, rotation_range, zoom_range,
                                                   horizontal_flip, vertical_flip, intensity_shift)

        if cropsize is not None:
            sample_images, target_labels = random_crop(sample_images, target_labels, cropsize)

        # sample coordinates should include the batch index for tf.gather_nd
        ind = []
        for cls in range(nclasses):
            pixels = np.stack(np.where(target_labels == cls), axis=1)
            idx = np.random.choice(range(pixels.shape[0]), int(batchsize*npix/nclasses), replace=True)
            ind.append(pixels[idx])

        ind = np.concatenate(ind, axis=0)
        ind = ind.reshape((batchsize,npix,3))

        coords = ind.astype(np.float32) / np.array([1, sample_images.shape[1], sample_images.shape[2]])

        bb, xx, yy = ind[:,:,0], ind[:,:,1], ind[:,:,2]
        pixel_labels = target_labels[bb,xx,yy]

        if categorical:
            # convert labels to categorical indicators for cross-entropy loss
            s = pixel_labels.shape
            pixel_labels = to_categorical(pixel_labels.flat, num_classes=nclasses)
            if np.any(confidence != 1.0):
                pixel_labels = smooth_labels(pixel_labels, confidence=confidence)
            pixel_labels = pixel_labels.reshape((s[0], s[1], nclasses))

        yield ([sample_images, coords], pixel_labels)
