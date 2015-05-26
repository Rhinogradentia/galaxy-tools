#!/usr/bin/env python
import argparse
from Bio import SeqIO
import StringIO
import logging
logging.basicConfig(level=logging.INFO)


def remove_qualifiers(genbank_file, feature_type=None, tag_type=None, tag_match=None):
    for record in SeqIO.parse(genbank_file, "genbank"):
        # Loop over features
        for feature in record.features:
            # If a feature_type is specified, use that.
            if feature_type is None or feature.type == feature_type:
                # Loop over qualifiers (e.g. /locus_tag, /product)
                good_qualifiers = {}
                for key in feature.qualifiers.keys():
                    # If we haven't specified a tag_type (i.e. match any) or we
                    # have specified one
                    if tag_type is None or key == tag_type:
                        # Feature qualifiers can be multiply valued. They will
                        # always return a list of values. (e.g. /note="A", /note="B")
                        acceptable = []
                        for value in feature.qualifiers[key]:
                            # If tag_match is any, match all keys. If tag_match
                            # is in that qualifier, match that.
                            if tag_match is None or tag_match in value:
                                pass
                            else:
                                acceptable.append(value)
                        good_qualifiers[key] = acceptable
                    else:
                        good_qualifiers[key] = feature.qualifiers[key]

                # Update the feature's qualifiers to "good" ones
                feature.qualifiers = good_qualifiers

        # Print out the genbank file
        handle = StringIO.StringIO()
        SeqIO.write([record], handle, "genbank")
        print handle.getvalue()


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description='Remove specific features from a Genbank file')
    parser.add_argument('genbank_file', type=file, help='Genbank file')
    parser.add_argument('--feature_type', help='Feature type to remove')
    parser.add_argument('--tag_type', help='Tag type to remove')
    parser.add_argument('--tag_match', help='String in tag to match')

    args = parser.parse_args()
    remove_qualifiers(**vars(args))