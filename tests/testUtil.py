import os
import random
import string
import re

from time import sleep

from ipwb import replay
from ipwb import indexer
from ipwb import __file__ as moduleLocation

from multiprocessing import Process

p = Process()


def createUniqueWARC():
    lines = []
    warcInFilename = 'frogTest.warc'
    warcInPath = os.path.join(os.path.dirname(__file__) + '/samples/warcs/' +
                              warcInFilename)

    stringToChange = b'abcdefghijklmnopqrstuvwxz'
    randomString = getRandomBytes(len(stringToChange))
    print('radon')
    print(randomString)

    with open(warcInPath, mode='rb') as warcFile:
        newContent = warcFile.read().replace(stringToChange, randomString)

    warcOutFilename = warcInFilename.replace('.warc', '_' +
                                             str(randomString) + '.warc')
    warcOutPath = os.path.join(os.path.dirname(__file__) +
                               '/samples/warcs/' + warcOutFilename)
    with open(warcOutPath, 'wb') as warcFile:
        warcFile.write(newContent)

    return warcOutPath


def getRandomString(n):
    return ''.join(random.SystemRandom().choice(
                   string.ascii_lowercase + string.digits) for _ in range(n))


def getRandomBytes(n):
    return bytes(getRandomString(n), 'utf-8')


def countCDXJEntries(cdxjData):
    urimCount = 0
    lines = cdxjData.strip().split('\n')
    for line in lines:
        if line[0] != '!':  # Exclude metadata from count
            urimCount += 1
    return urimCount


def startReplay(warcFilename):
    global p
    pathOfWARC = os.path.join(os.path.dirname(moduleLocation) +
                              '/samples/warcs/' + warcFilename)
    tempFilePath = '/tmp/' + ''.join(random.sample(
        string.ascii_uppercase + string.digits * 6, 12)) + '.cdxj'

    open(tempFilePath, 'a').close()  # Create placeholder file for replay

    p = Process(target=replay.start, args=[tempFilePath])
    p.start()
    sleep(5)

    cdxjList = indexer.indexFileAt(pathOfWARC, quiet=True)
    cdxj = '\n'.join(cdxjList)

    with open(tempFilePath, 'w') as f:
        f.write(cdxj)


def stopReplay():
    global p
    p.terminate()


def extractRelationEntriesFromLinkTimeMap(tm):
    matches = re.findall('rel=".*?"', tm)
    matches = map(lambda s: s[5:-1], matches)
    return matches
