#!/usr/bin/env python
import sys
import logging
import argparse
from Bio import SeqIO
from BCBio import GFF
from gff3 import feature_lambda, feature_test_type
logging.basicConfig(level=logging.INFO)
log = logging.getLogger(__name__)

def require_shinefind(gff3, fasta):
    # Load up sequence(s) for GFF3 data
    seq_dict = SeqIO.to_dict(SeqIO.parse(fasta, "fasta"))
    # Parse GFF3 records
    for record in GFF.parse(gff3, base_dict=seq_dict):
        # Reopen
        cdss = list(feature_lambda(record.features, feature_test_type, {'type': 'CDS'}, subfeatures=True))
        good_cdss = []
        for cds in cdss:
            if cds.extract(record).seq[0:3].upper() in ('GTG', 'ATG', 'TTG'):
                good_cdss.append(cds)

        record.features = good_cdss
        yield record

if __name__ == '__main__':
    parser = argparse.ArgumentParser(description='Require specific start codons')
    parser.add_argument('fasta', type=file, help='Fasta Genome')
    parser.add_argument('gff3', type=file, help='GFF3 annotations')
    args = parser.parse_args()

    for rec in require_shinefind(**vars(args)):
        GFF.write([rec], sys.stdout)