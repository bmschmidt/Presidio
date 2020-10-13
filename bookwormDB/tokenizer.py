#! /usr/bin/python

from __future__ import print_function
import random
import sys
import os
from .sqliteKV import KV
import time
import logging
import numpy as np
from pandas import read_csv
from io import StringIO
import re

"""
This section does a lot of work on tokenizing and aggregating wordcounts.
"""

# Likewise, store a thread-wise count on whether we've thrown a unicode encoding error.
haveWarnedUnicode = False
# And the default regex is generated by a function on demand.
bigregex = None


def wordRegex():
    """
    #I'm including the code to create the regex, which makes it more readable.
    Note that this uses *unicode*: among other things, that means that it needs to be passed
    a unicode-decoded string: and that we have to use the "regex" module instead of the "re" module. Python3 will make this, perhaps, easier.
    """
    global re
    MasterExpression = r"\w+"
    possessive = MasterExpression + r"'s"
    numbers = r"(?:[\$])?\d+"
    decimals = numbers + r"\.\d+"
    abbreviation = r"(?:mr|ms|mrs|dr|prof|rev|rep|sen|st|sr|jr|ft|gen|adm|lt|col|etc)\."
    sharps = r"[a-gjxA-GJX]#"
    punctuators = r"[^\w\p{Z}]"
    """
    Note: this compiles looking for the most complicated words first, and as it goes on finds simpler and simpler forms 
    """
    bigregex = re.compile("|".join([decimals,possessive,numbers,abbreviation,sharps,punctuators,MasterExpression]),re.UNICODE|re.IGNORECASE)
    return bigregex


def readDictionaryFile(prefix=""):
    look = dict()
    for line in open(prefix + ".bookworm/texts/wordlist/wordlist.txt"):
        line = line.rstrip("\n")
        v, k, _ = line.split("\t")
        look[k] = v
    return look

def readIDfile(prefix=""):
    if not os.path.exists(".bookworm/metadata/textids.sqlite"):
        raise FileNotFoundError("No textids DB: run `bookworm build textids`")
    return KV(prefix + ".bookworm/metadata/textids.sqlite")

class tokenBatches(object):
    """
    A tokenBatches is a manager for tokenizers. Each one corresponds to 
    a reasonable number of texts to read in to memory on a single processor:
    during the initial loads, there will probably be one per core.
    It doesn't store the original text, just the unigram and bigram tokenizations in its attached self.counts arrays.
    
    It writes out its dat to a single file: 
       in this way, a batch of up to several hundred thousand individual files is grouped into a single file.

    It also has a method that encodes and writes its wordcounts into a tsv file appropriate for reading with mysql,
    with 3-byte integer encoding for wordid and bookid.
    """
    
    def __init__(self, levels=["unigrams","bigrams"]):
        """
        
        mode: 'encode' (write files out)
        """
        self.id = '%030x' % random.randrange(16**30)
        self.levels=levels

        # placeholder to alert that createOutputFiles must be run.
        self.completedFile = None
        
    def createOutputFiles(self):
        self.completedFile = open(".bookworm/texts/encoded/completed/" + self.id,"w")
        self.outputFiles = dict()
        for level in self.levels:
            self.outputFiles[level] = open(".bookworm/texts/encoded/{}/{}.txt".format(level, self.id),"w")
        
    def attachDictionaryAndID(self):
        self.dictionary = readDictionaryFile()
        self.IDfile = readIDfile()


    def close(self):
        """
        This test allows the creation of bookworms with fewer document than requested 
        threads, which happens to be the case in the tests.
        """
        if self.completedFile is not None:
            self.completedFile.close()
            for v in self.outputFiles.values():
                v.close()
        
    def encodeRow(self,
                  filename,
                  tokenizer,
                  write_completed=True
    ):
        """
        'id': the filename
        'tokenizer': a tokenizer object

        """
        if self.completedFile is None:
            self.createOutputFiles()
            self.attachDictionaryAndID()
            
        #The dictionary and ID lookup tables should be pre-attached.
        dictionary = self.dictionary
        IDfile = self.IDfile

        levels = None
        """
        if source=="raw_text":
            parts = row.split("\t", 1)
            filename = parts[0]
            try:
                tokens = tokenizer(parts[1])
            except IndexError:
                logging.warn("\nFound no tab in the input for '" + filename + "'...skipping row\n")
            levels = self.levels

        if source == "countfile":
            try:
                (filename, token, count) = row.split("\t")
            except:
                logging.error("Can't find tab\n***************")
                logging.error(row)
                raise
            tokens = preTokenized(token, count, self.levels[0])
        """
        
        try:
            textid = IDfile[filename]
        except KeyError:
            logging.warn("Warning: file " + filename + " not found in jsoncatalog.txt, not encoding")
            return

        for level in self.levels:
            outputFile = self.outputFiles[level]
            output = []

            counts = tokenizer.counts(level)

            for wordset, count in counts.items():
                skip = False
                wordList = []
                for word in wordset:
                    try:
                        wordList.append(dictionary[word])
                    except KeyError:
                        """
                        if any of the words to be included is not in the dictionary,
                        we don't include the whole n-gram in the counts.
                        """
                        skip = True                        
                if not skip:
                    wordids = "\t".join(wordList)
                    output.append("{}\t{}\t{}".format(int(textid), wordids, count))

            try:
                if len(output) > 0:
                    # The test is necessary because otherwise this prints a blank line.
                    outputFile.write("\n".join(output) + "\n")
                
            except IOError as e:
                logging.exception(e)

        if write_completed:
            self.completedFile.write(filename + "\n")

class Tokenizer(object):
    """
    A tokenizer is initialized with a single text string.

    It assumes that you have in namespace an object called "bigregex" which
    identifies words.

    (I'd define it here, but it's a performance optimization to avoid compiling the large regex millions of times.)

    the general way to call it is to initialize, and then for each desired set of counts call "tokenizer.counts("bigrams")" (or whatever).

    That returns a dictionary, whose keys are tuples of length 1 for unigrams, 2 for bigrams, etc., and whose values are counts for that ngram. The tuple form should allow faster parsing down the road.
    
    """
    
    def __init__(self, string, tokenization_regex=None):
        global haveWarnedUnicode
        self.string = string
        self.tokenization_regex = tokenization_regex
        self.tokens = None
    def tokenize(self):
        """
        This tries to return the pre-made tokenization:
        if that doesn't exist, it creates it.
        """
        if self.tokens is not None:
            return self.tokens
        """
        For speed, don't import until here.
        """
        tokenization_regex=self.tokenization_regex
        global re
        if re is None:
            import regex as re
        if tokenization_regex is None:
            # by default, use the big regex.
            global bigregex
            if bigregex==None:
                bigregex = wordRegex()
            tokenization_regex = bigregex
        self.tokens = re.findall(tokenization_regex, self.string)
        return self.tokens

    def ngrams(self, n, collapse = False):
        """
        All the ngrams in the text can be created as a tuple by zipping an arbitrary number of
        copies of the text to itself.
        """
        
        self.tokenize()
        l = list(zip(*[self.tokens[i:] for i in range(n)]))
        if collapse:
            l = [" ".join(tupled) for tupled in l]
        return l

    def unigrams(self):
        return self.ngrams(1)

    def bigrams(self):
        return self.ngrams(2)

    def trigrams(self):
        return self.ngrams(3)

    def allgrams(self, max = 6):
        output = []
        for i in range(1, max + 1):
            output.extend(self.ngrams(i, collapse = True))
        return output

    def words(self):
        """
        1-grams have tuple keys, but words have index keys.
        """
        self.tokenize()
        return self.tokens
    
    def counts(self, whichType):
        
        count = dict()
        for gram in getattr(self,whichType)():
            try:
                count[gram] += 1
            except KeyError:
                count[gram] = 1
        return count


class PreTokenized(object):
    """
    This class is a little goofy: it mimics the behavior of a tokenizer
    one data that's already been tokenized by something like
    Google Ngrams or JStor Data for Research.
    """

    def __init__(self, csv_string, level):
        f = read_csv(StringIO(csv_string),
                     lineterminator = "\f",
                     # Ugh--want 'NA' to be a word.
                     dtype = {'word': str, 'counts': np.int},
                     keep_default_na=False,
                     names = ["word", "counts"])
        self.level = level
        if level == 'words':
            self.output = dict(zip(f.word, f.counts))
        else:
            self.output = dict(zip([tuple(w.split(" ")) for w in f.word], f.counts))
            
    def counts(self,level):
        if level != self.level:
            raise
        return self.output

    
def getAlreadySeenList(folder):
    #Load in a list of what's already been translated for that level.
    #Returns a set.
    files = os.listdir(folder)
    seen = set([])
    for file in files:
        for line in open(folder + "/" + file):
            seen.add(line.rstrip("\n"))
    return seen

def encode_text_stream():
    seen = getAlreadySeenList(".bookworm/texts/encoded/completed")
    tokenBatch = tokenBatches()
    tokenBatch.attachDictionaryAndID()
    for line in sys.stdin:
        filename = line.split("\t",1)[0]
        line = line.rstrip("\n")
        if filename not in seen:
            tokenBatch.encodeRow(line)
            
    # And printout again at the end

if __name__=="__main__":
    encode_text_stream()

