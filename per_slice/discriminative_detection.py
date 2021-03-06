#!/usr/bin/python
""" Discriminative per-slice detection. """
import getopt
from ipdb import set_trace
from itertools import izip
import multiprocessing as mp
import numpy as np
import os
import sys

from dataset import Dataset

from do import _get_samples

from sklearn.grid_search import GridSearchCV
from sklearn.cross_validation import StratifiedShuffleSplit
from sklearn import svm

from fisher_vectors.data import SstatsMap
from fisher_vectors.evaluation.utils import average_precision
from fisher_vectors.evaluation.utils import tuple_labels_to_list_labels
from fisher_vectors.model.utils import standardize
from fisher_vectors.model.utils import power_normalize
from fisher_vectors.model.utils import compute_L2_normalization
from fisher_vectors.model.fv_model import FVModel
from fisher_vectors.preprocess.gmm import load_gmm


CHUNK_SIZE = 300
FILE = '/home/lear/oneata/tmp/%s_train_per_video_class_%d_iteration_0.dat'
RESULT_FILE = ('/home/lear/oneata/data/trecvid11/results/'
               'retrain_feature_pooling_with_background_info.txt')


def usage():
    pass


def aggregate(sstats, weights, limits):
    """ Aggregate sufficient statistics using given weights. """
    nr_features = sstats.shape[1]
    nr_samples = len(limits) - 1
    aggregated = np.zeros((nr_samples, nr_features))
    for ii, (low, high) in enumerate(izip(limits[: -1], limits[1:])):
        aggregated[ii, : nr_features] = _weighted_sum(
            sstats[low: high], weights[low: high])
    return aggregated


def chunker(seq, size):
    return (seq[pos:pos + size] for pos in xrange(0, len(seq), size - 1))


def _weighted_sum(sstats, weights):
    return np.sum(sstats * weights[:, np.newaxis], axis=0)


def _norm(values, norm_type):
    assert norm_type in ('L0', 'L1')
    if norm_type == 'L1':
        return np.sum(values)
    elif norm_type == 'L0':  # Not exactly L0.
        return len(values)


def _normalize(values, limits, norm_type='L1'):
    assert values.ndim == 1, "The values vector should be one-dimensional."

    new_values = np.zeros_like(values)
    for low, high in izip(limits[: -1], limits[1:]):
        new_values[low: high] = values[low: high] / _norm(values[low: high],
                                                          norm_type)
    return new_values


def _extend_per_slice(values, limits):
    """ Duplicates the values acording to limits. """
    extended_values = []
    for value, low, high in izip(values, limits[: -1], limits[1:]):
        extended_values += [value] * (high - low)
    return extended_values


def get_slice_data_from_file(dataset, split, class_idx, gmm, nr_pos, nr_neg):
    samples = _get_samples(dataset, class_idx, data_type=split, nr_pos=nr_pos,
                           nr_neg=nr_neg)[0]
    len_descs = gmm.k + 2 * gmm.d * gmm.k
    sstats, labels, info = SstatsMap(
        os.path.join(dataset.SSTATS_DIR, 'stats.tmp')).get_merged(
            samples, len_descs)
    sstats = sstats.reshape((-1, len_descs))
    binary_labels = tuple_labels_to_list_labels(labels, class_idx)
    return SliceData(sstats, binary_labels, info)


class SliceData(object):
    def __init__(self, sstats, labels, info):
        self.sstats = sstats
        self.labels = labels
        self.video_limits = np.array(info['limits'])
        self.video_names = _extend_per_slice(
            info['video_names'], self.video_limits)
        self.nr_descs = info['nr_descs']
        self.begin_frames = info['begin_frames']
        self.end_frames = info['end_frames']
        self.nr_slices, self.nr_features = self.sstats.shape
        self.scores = 0.5 * np.ones(self.nr_slices)  # Initial scores.

    def get_aggregated(self, agg_type, use_nr_descs):
        if use_nr_descs: 
            norm_type = 'L1'
            if agg_type == 'norm':
                weights = _normalize(
                    self.scores * self.nr_descs, self.video_limits, norm_type)
                weights_bar = _normalize(
                    (1. - self.scores) * self.nr_descs,
                    self.video_limits, norm_type)
            elif agg_type == 'unnorm':
                norm_nr_descs = _normalize(
                    self.nr_descs, self.video_limits, 'L1')
                weights = self.scores * norm_nr_descs
                weights_bar = (1 - self.scores) * norm_nr_descs
        else:
            if agg_type == 'norm':
                norm_type = 'L1'
            elif agg_type == 'unnorm':
                norm_type = 'L0'
            weights = _normalize(self.scores, self.video_limits, norm_type)
            weights_bar = _normalize(1. - self.scores, self.video_limits,
                                     norm_type)
        
        return [aggregate(self.sstats, weights, self.video_limits),
                aggregate(self.sstats, weights_bar, self.video_limits)]
                #aggregate(self.sstats, self.nr_descs, self.video_limits)]

    def get_aggregated_by_nr_descs(self):
        weights = _normalize(self.nr_descs, self.video_limits, 'L1')
        return aggregate(self.sstats, weights, self.video_limits)

    def get_sample_labels(self):
        sample_labels = []
        for ii in self.video_limits[: -1]:
            sample_labels.append(self.labels[ii])
        return np.array(sample_labels)

    def update_scores(self, clf, model):
        predictions = []
        for limits in chunker(self.video_limits, CHUNK_SIZE):
            # Augment sufficient statistics with background information.
            low = limits[0]
            high = limits[-1]
            # Use original slices, without weighting.
            sstats_list = [self.sstats[low: high]] * 2
            # Augmenting with original features.
            #sstats_list = [self.sstats[low: high]] * 3
            # Weight each slice by the normalized score.
            #sstats_list = [
            #    self.sstats[low: high] * _normalize(
            #        self.scores[low: high], limits - low)[:, np.newaxis],
            #    self.sstats[low: high] * _normalize(
            #        1. - self.scores[low: high], limits - low)[:, np.newaxis]
            #]
            # Weight each slice by the un-normalized score.
            #sstats_list = [
            #    chunk * chunk_scores[:, np.newaxis],
            #    chunk * (1. - chunk_scores[:, np.newaxis])]
            # Compute kernel.
            te_kernel = model.get_te_kernel(sstats_list)
            # Predict on them.
            # TODO Pre-allocate!
            predictions.append(clf.predict(te_kernel))
        self.scores = np.hstack(predictions)

    def save_htlist(self, filename):
        pass


class Model(object):
    def __init__(self, gmm):
        self.gmm = gmm
        self.D = gmm.k + 2 * gmm.k * gmm.d
        self.xx = []
        self.mu = []
        self.sigma = []

    def _append_data(self, xx, mu, sigma):
        self.xx.append(xx)
        self.mu.append(mu)
        self.sigma.append(sigma)

    def get_tr_kernel(self, sstats_list):
        self.N_tr = sstats_list[0].reshape((-1, self.D)).shape[0]
        # Initialise train kernel.
        tr_kernel = np.zeros((self.N_tr, self.N_tr))
        # Initialise normalization constants.
        self.Zx = np.zeros(self.N_tr)
        for ii, sstats in enumerate(sstats_list):
            self._append_data(
                *standardize(FVModel.sstats_to_features(sstats, self.gmm)))
            self.xx[ii] = power_normalize(self.xx[ii], 0.5)
            self.Zx += compute_L2_normalization(self.xx[ii])
            tr_kernel += np.dot(self.xx[ii], self.xx[ii].T)
        # Normalize kernel.
        tr_kernel /= np.sqrt(
            self.Zx[:, np.newaxis] * self.Zx[np.newaxis])
        return tr_kernel

    def get_te_kernel(self, sstats_list):
        self.N_te = sstats_list[0].reshape((-1, self.D)).shape[0]
        # Initialise train kernel.
        te_kernel = np.zeros((self.N_te, self.N_tr))
        # Initialise normalization constants.
        self.Zy = np.zeros(self.N_te)
        for ii, sstats in enumerate(sstats_list):
            yy = standardize(
                FVModel.sstats_to_features(sstats, self.gmm), self.mu[ii],
                self.sigma[ii])[0]
            yy = power_normalize(yy, 0.5)
            self.Zy += compute_L2_normalization(yy)
            te_kernel += np.dot(yy, self.xx[ii].T)
        # Normalize kernel.
        te_kernel /= np.sqrt(
            self.Zy[:, np.newaxis] * self.Zx[np.newaxis])
        return te_kernel


class MySVC(svm.SVC):
    def predict(self, X):
        return self.decision_function(X)


class Evaluation(object):
    def __init__(self):
        pass

    def fit(self, tr_kernel, tr_labels):
        my_svm = MySVC(kernel='precomputed', probability=True)
        tuned_parameters = {
            'C': np.power(3.0, np.arange(-2, 8)),
            'class_weight': [{-1: 1, 1: 2 ** jj} for jj in xrange(10)]}

        splits = StratifiedShuffleSplit(tr_labels, 5, test_size=0.25,
                                        random_state=1)

        self.clf = GridSearchCV(
            my_svm, tuned_parameters, score_func=average_precision,
            cv=splits, n_jobs=8)
        self.clf.fit(tr_kernel, tr_labels)
        return self

    def predict(self, te_kernel):
        return self.clf.predict_proba(te_kernel)[:, 1]

    def score(self, te_kernel, te_labels):
        predicted = self.predict(te_kernel)
        score = average_precision(te_labels, predicted) * 100
        return score


def discriminative_detection_per_class(class_idx, **kwargs):
    max_nr_iter = kwargs.get('max_nr_iter', 1)
    #dataset = kwargs.get('dataset', Dataset(
    #    'trecvid11_small', nr_clusters=128, suffix='.small.per_slice.delta_240'))
        #'trecvid11_small', nr_clusters=128, suffix='.small.per_slice'))
    src_cfg = kwargs.get('src_cfg')
    nr_clusters = kwargs.get('nr_clusters')
    suffix = kwargs.get('suffix')
    outfile = kwargs.get('outfile', FILE % (src_cfg, class_idx))
    nr_pos = kwargs.get('nr_pos', 10000)
    nr_neg = kwargs.get('nr_neg', 10000)
    agg_type = kwargs.get('agg_type', 'norm')
    use_nr_descs = kwargs.get('use_nr_descs', False)

    assert agg_type in ('norm', 'unnorm'), "Unknown aggregation type."

    dataset = Dataset(src_cfg, nr_clusters=nr_clusters, suffix=suffix)
    gmm = load_gmm(dataset.GMM)
    tr_slice_data = get_slice_data_from_file(
        dataset, 'train', class_idx, gmm, nr_pos, nr_neg)
    te_slice_data = get_slice_data_from_file(
        dataset, 'test', class_idx, gmm, nr_pos, nr_neg)
    tr_sample_labels = tr_slice_data.get_sample_labels()
    te_sample_labels = te_slice_data.get_sample_labels()
    for ii in xrange(max_nr_iter):
        print 'Iteration %d' % ii
        # Feature pooling.
        model = Model(gmm)
        if ii == 0:
            if not os.path.exists(outfile):
                print 'Aggregating statistics by the number of descriptors...'
                ss = tr_slice_data.get_aggregated_by_nr_descs()
                np.array(ss, dtype=np.float32).tofile(outfile)
                #tr_sample_sstats = [ss, ss]
                tr_sample_sstats = [ss] * 2
            else:
                print 'Loaded aggregated statistics...'
                # Cache results.
                ss = np.fromfile(
                    outfile, dtype=np.float32).reshape((-1, model.D))
                #tr_sample_sstats = [ss, ss]
                tr_sample_sstats = [ss] * 2
        else:
            tr_sample_sstats = tr_slice_data.get_aggregated(
                agg_type, use_nr_descs)
        # Fisher vectors on pooled features.
        tr_kernel = model.get_tr_kernel(tr_sample_sstats)
        # Train classifier on pooled features.
        _eval = Evaluation()
        _eval = _eval.fit(tr_kernel, tr_sample_labels)
        # Update weights.
        tr_slice_data.update_scores(_eval, model)
        te_slice_data.update_scores(_eval, model)
        # TODO Save data.
        tr_slice_data.save_htlist(ii)
        te_slice_data.save_htlist(ii)
        del _eval
        del model
    # Final retraining and evaluation.
    tr_sample_sstats = tr_slice_data.get_aggregated(agg_type, use_nr_descs)
    te_sample_sstats = te_slice_data.get_aggregated(agg_type, use_nr_descs)
    model = Model(gmm)
    tr_kernel = model.get_tr_kernel(tr_sample_sstats)
    te_kernel = model.get_te_kernel(te_sample_sstats)
    _eval = Evaluation()
    _eval = _eval.fit(tr_kernel, tr_sample_labels)
    score = _eval.score(te_kernel, te_sample_labels)
    print 'Class %d score %2.3f' % (class_idx, score)
    return score


def discriminative_detection(start_idx=0, end_idx=15, **kwargs):
    nr_processes = kwargs.get('nr_processes', 1)
    classes_per_process = int(
        np.ceil(float(end_idx - start_idx) / nr_processes))
    processes = []

    print start_idx, end_idx
    if nr_processes > 1:
        for ii in xrange(nr_processes):
            ss = start_idx + ii * classes_per_process
            ee = min(end_idx, start_idx + (ii + 1) * classes_per_process)
            process = mp.Process(
                target=discriminative_detection_worker, args=(ss, ee),
                kwargs=kwargs)
            processes.append(process)
            process.start()

        for process in processes:
            process.join()
    else:
        discriminative_detection_worker(start_idx, end_idx, **kwargs)


def discriminative_detection_worker(start_idx, end_idx, **kwargs):
    for ii in xrange(start_idx, end_idx):
        score = discriminative_detection_per_class(ii, **kwargs)
        ff = open(RESULT_FILE, 'a')
        ff.write('Class %d score %2.3f\n' % (ii, score))
        ff.close()


def main():
    try:
        opt_pairs, _args = getopt.getopt(
            sys.argv[1:], "hs:e:d:k:",
            ["help", "start_idx=", "end_idx=", "dataset=", "nr_clusters=",
             "suffix=", "use_nr_descs", "agg_type=", "nr_pos=", "nr_neg=",
             "nr_processes="])
    except getopt.GetoptError, err:
        print str(err)
        usage()
        sys.exit(1)

    kwargs = {}
    for opt, arg in opt_pairs:
        if opt in ("-h", "--help"):
            usage()
            sys.exit()
        elif opt in ("-s", "--start_idx"):
            start_idx = int(arg)
        elif opt in ("-e", "--end_idx"):
            end_idx = int(arg)
        elif opt in ("-d", "--dataset"):
            kwargs['src_cfg'] = arg
        elif opt in ("-k", "--nr_clusters"):
            kwargs['nr_clusters'] = int(arg)
        elif opt in ("--suffix"):
            kwargs['suffix'] = arg
        elif opt in ("--use_nr_descs"):
            kwargs['use_nr_descs'] = True
        elif opt in ("--agg_type"):
            kwargs['agg_type'] = arg
        elif opt in ("--nr_pos"):
            kwargs['nr_pos'] = int(arg)
        elif opt in ("--nr_neg"):
            kwargs['nr_neg'] = int(arg)
        elif opt in ("--nr_processes"):
            kwargs['nr_processes'] = int(arg)

    discriminative_detection(start_idx, end_idx, **kwargs)


if __name__ == '__main__':
    main()

# Profiling
#import profile
#for ii in xrange(1):
#    profile.run('discriminative_detection_worker(%d)' % ii,
#                'timings_%d.stats' % ii)
