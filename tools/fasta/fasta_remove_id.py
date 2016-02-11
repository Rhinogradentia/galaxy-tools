#!/usr/bin/env python
import sys
import argparse
from Bio import SeqIO
import logging
logging.basicConfig(level=logging.INFO)
log = logging.getLogger()

def drop_id(fasta_file=None, id="drop_idd"):
    for rec in SeqIO.parse(fasta_file, "fasta"):
        rec.description = ''
        yield rec


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description='Identify shine-dalgarno sequences')
    parser.add_argument('fasta_file', type=file, help='Genbank file')

    args = parser.parse_args()
    for rec in drop_id(**vars(args)):
        SeqIO.write([rec], sys.stdout, "fasta")
