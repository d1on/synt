# -*- coding: utf-8 -*-
"""Functions to interact with databases."""
import os
import sqlite3
import redis
import cPickle as pickle
from synt import settings
from nltk.probability import ConditionalFreqDist, FreqDist
from nltk.metrics import BigramAssocMeasures
from synt.utils.text import normalize_text
from synt.utils.processing import batch_job

def db_exists(name):
    path = os.path.join(os.path.expanduser(settings.DB_PATH), name)
    return True if os.path.exists(path) else False
    
def db_init(db='samples.db', create=True):
    """Initializes the sqlite3 database."""
    if not os.path.exists(os.path.expanduser(settings.DB_PATH)):
        os.makedirs(os.path.expanduser(settings.DB_PATH))

    fp = os.path.join(os.path.expanduser(settings.DB_PATH), db)
    
    if not db_exists(db):
        conn = sqlite3.connect(fp)
        cursor = conn.cursor()
        if create:
            cursor.execute('''CREATE TABLE item (id integer primary key, text text unique, sentiment text)''')
    else:
        conn = sqlite3.connect(fp)
    return conn


def redis_feature_consumer(samples, r):
    """
    Stores counts to redis via a pipeline.
    """
   
    pipeline = r.pipeline()

    neg_processed, pos_processed = 0, 0

    for text, label in samples:
        
        count_label = label + '_wordcounts'

        tokens = normalize_text(text)

        if tokens:
            if label.startswith('pos'):
                pos_processed += 1
            else:
                neg_processed += 1

            for word in set(tokens): #make sure we only add word once
                pipeline.zincrby(count_label, word)

    pipeline.incr('negative_processed', neg_processed) 
    pipeline.incr('positive_processed', pos_processed)
    
    pipeline.execute()

class RedisManager(object):

    def __init__(self, db=5, host='localhost', purge=False):
        self.r = redis.Redis(db=db, host=host)
        if purge: self.r.flushdb()


    def store_freqdists(self):
        """
        Store features with counts to Redis.
        """

        word_fd = FreqDist()
        label_word_freqdist = ConditionalFreqDist()

        pos_words = self.r.zrange('positive_wordcounts', 0, -1, withscores=True, desc=True)
        neg_words = self.r.zrange('negative_wordcounts', 0, -1, withscores=True, desc=True)

        assert pos_words and neg_words, 'Requires wordcounts to be stored in redis.'

        #build a condtional freqdist with the feature counts per label
        for word, count in pos_words:
            word_fd.inc(word, count)
            label_word_freqdist['positive'].inc(word, count)

        for word,count in neg_words:
            word_fd.inc(word, count)
            label_word_freqdist['negative'].inc(word, count)

        self.pickle_store('word_fd', word_fd)
        self.pickle_store('label_fd', label_word_freqdist)
        

    def store_feature_counts(self, samples, chunksize=10000, processes=None):
        """
        Stores feature:count histograms for samples in Redis with the ability to increment.
       
        Arguments:
        samples             -- a list of samples in the format (text, label)

        Keyword Arguments:
        chunksize           -- the amount of samples to process at a time
        processes           -- the amount of processors to use with multiprocessing  
        """

        if 'positive_wordcounts' and 'negative_wordcounts' in self.r.keys():
            return

        #def producer(offset, length):
        #    if offset + length > samples:
        #        length = samples - offset
        #    if length < 1:
        #        return []
        #    return get_samples(length, offset=offset)

        batch_job(samples, redis_feature_consumer(samples, self.r), chunksize, processes)
        
    def store_feature_scores(self):
        """
        Stores feature scores to Redis.
        """
        
        try:
            word_fd = self.pickle_load('word_fd')
            label_word_freqdist = self.pickle_load('label_fd')
        except TypeError:
            print('Requires frequency distributions to be built.')

        word_scores = {}

        pos_word_count = label_word_freqdist['positive'].N()
        neg_word_count = label_word_freqdist['negative'].N()
        total_word_count = pos_word_count + neg_word_count

        for label in label_word_freqdist.conditions():

            for word, freq in word_fd.iteritems():

                pos_score = BigramAssocMeasures.chi_sq(label_word_freqdist['positive'][word], (freq, pos_word_count), total_word_count)
                neg_score = BigramAssocMeasures.chi_sq(label_word_freqdist['negative'][word], (freq, neg_word_count), total_word_count)
            
                word_scores[word] = pos_score + neg_score 
      
        self.pickle_store('word_scores', word_scores)

    def pickle_store(self, name, data):
        self.r.set(name, pickle.dumps(data))

    def pickle_load(self, name):
        try:
            return pickle.loads(self.r.get(name))
        except TypeError:
            return
    
    def store_classifier(self, name, classifier):
        """
        Stores a pickled a classifier into Redis.
        """
        self.pickle_store(name, classifier)

    def load_classifier(self, name):
        """
        Loads (unpickles) a classifier from Redis.
        """
        try:
            return self.pickle_load(name)    
        except TypeError:
            return     

    def store_best_features(self, n=10000):
        """Store n best features to Redis."""
        if not n: return

        word_scores = self.pickle_load('word_scores')

        assert word_scores, "Word scores need to exist."
        
        best = sorted(word_scores.iteritems(), key=lambda (w,s): s, reverse=True)[:n]

        self.pickle_store('best_words',  best)
        
    def get_best_features(self):
        """
        Return stored best features.
        """
        best_words = self.pickle_load('best_words')

        if best_words:
            return set([word for word,score in best_words])

def get_sample_limit(db='samples.db'):
    """
    Returns the limit of samples so that both positive and negative samples
    will remain balanced.
    """

    #this is an expensive operation in case of a large database
    #therefore we store the limit in redis and use that when we can
    m = RedisManager()
    if 'limit' in m.r.keys():
        return int(m.r.get('limit'))

    db = db_init(db=db)
    cursor = db.cursor()
    cursor.execute("SELECT COUNT(*) FROM item where sentiment = 'positive'")
    pos_count = cursor.fetchone()[0]
    cursor.execute("SELECT COUNT(*) FROM item where sentiment = 'negative'")
    neg_count = cursor.fetchone()[0]
    if neg_count > pos_count:
        limit = pos_count
    else:
        limit = neg_count
    
    #store to redis
    m.r.set('limit', limit)
    
    return limit

def get_samples(db, limit, offset=0):
    """
    Returns a combined list of negative and positive samples.
    """

    db = db_init(db=db)
    cursor = db.cursor()

    sql =  "SELECT text, sentiment FROM item WHERE sentiment = ? LIMIT ? OFFSET ?"

    if limit < 2: limit = 2

    if limit > get_sample_limit():
        limit = get_sample_limit()

    if not limit % 2 == 0:
        limit -= 1 #we want an even number
    
    limit = limit / 2 
    offset = offset / 2

    cursor.execute(sql, ["negative", limit, offset])
    neg_samples = cursor.fetchall()

    cursor.execute(sql, ["positive", limit, offset])
    pos_samples = cursor.fetchall()

    return pos_samples + neg_samples

