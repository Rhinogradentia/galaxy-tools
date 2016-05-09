#!/usr/bin/env python
# vim: set fileencoding=utf-8
import os
import json
import math
import argparse
import itertools
from gff3 import feature_lambda, feature_test_type, feature_test_quals, \
    coding_genes, genes, get_gff3_id, feature_test_location
from shinefind import NaiveSDCaller
from BCBio import GFF
from Bio.Data import CodonTable
from Bio import SeqIO
from Bio.SeqRecord import SeqRecord
from Bio.Seq import reverse_complement, translate
from Bio.SeqFeature import SeqFeature, FeatureLocation
from jinja2 import Environment, FileSystemLoader
import itertools
from cpt import OrfFinder
import re
import logging
logging.basicConfig(level=logging.DEBUG)
log = logging.getLogger(name='pav')

# Path to script, required because of Galaxy.
SCRIPT_PATH = os.path.dirname(os.path.realpath(__file__))
# Path to the HTML template for the report

ENCOURAGEMENT = (
    (100, 'Perfection itself!'),
    (90, 'Amazing!'),
    (80, 'Not too bad, a few minor things to fix...'),
    (70, 'Some issues to address'),
    (50, """Issues detected! </p><p class="text-muted">Have you heard of the
     <a href="https://cpt.tamu.edu">CPT</a>\'s Automated Phage Annotation
     Pipeline?"""),
    (0, """<b>MAJOR</b> issues detected! Please consider using the
     <a href="https://cpt.tamu.edu">CPT</a>\'s Automated Phage Annotation Pipeline"""),
)


def gen_qc_feature(start, end, message, strand=0, id_src=None):
    kwargs = {
        'qualifiers': {
            'note': [message]
        }
    }
    if id_src is not None:
        kwargs['id'] = id_src.id
        kwargs['qualifiers']['Name'] = id_src.qualifiers.get('Name', [])

    return SeqFeature(
        FeatureLocation(start, end, strand=strand),
        **kwargs
    )


def __ensure_location_in_bounds(start=0, end=0, parent_length=0):
    # This prevents frameshift errors
    while start < 0:
        start += 3
    while end < 0:
        end += 3
    while start > parent_length:
        start -= 3
    while end > parent_length:
        end -= 3
    return (start, end)

def get_rbs_from(gene):
    # Normal RBS annotation types
    rbs_rbs = list(feature_lambda(gene.sub_features, feature_test_type, {'type': 'RBS'}, subfeatures=False))
    rbs_sds = list(feature_lambda(gene.sub_features, feature_test_type, {'type': 'Shine_Dalgarno_sequence'}, subfeatures=False))
    # Fraking apollo
    apollo_exons = list(feature_lambda(gene.sub_features, feature_test_type, {'type': 'exon'}, subfeatures=False))
    apollo_exons = [x for x in apollo_exons if len(x) < 10]
    # These are more NCBI's style
    regulatory_elements = list(feature_lambda(gene.sub_features, feature_test_type, {'type': 'regulatory'}, subfeatures=False))
    rbs_regulatory = list(feature_lambda(regulatory_elements, feature_test_quals, {'regulatory_class': ['ribosome_binding_site']}, subfeatures=False))
    # Here's hoping you find just one ;)
    return rbs_rbs + rbs_sds + rbs_regulatory + apollo_exons

def missing_rbs(record, lookahead_min=5, lookahead_max=15):
    """
    Identify gene features with missing RBSs

    This "looks ahead" 5-15 bases ahead of each gene feature, and checks if
    there's an RBS feature in those bounds.

    The returned data is a set of genes with the RBS sequence in the __upstream
    attribute, and a message in the __message attribute.
    """
    results = []
    good = 0
    bad = 0
    qc_features = []
    sd_finder = NaiveSDCaller()

    any_rbss = False

    for gene in coding_genes(record.features):
        # Check if there are RBSs, TODO: make this recursive. Each feature in
        # gene.sub_features can also have sub_features.
        rbss = get_rbs_from(gene)
        # No RBS found
        if len(rbss) == 0:
            # Get the sequence lookahead_min to lookahead_max upstream
            if gene.strand > 0:
                start = gene.location.start - lookahead_max
                end = gene.location.start - lookahead_min
            else:
                start = gene.location.end + lookahead_min
                end = gene.location.end + lookahead_max
            # We have to ensure the feature is ON the genome, otherwise we may
            # be trying to access a location outside of the length of the
            # genome, which would be bad.
            (start, end) = __ensure_location_in_bounds(start=start, end=end,
                                                       parent_length=record.__len__)
            # Temporary feature to extract sequence
            tmp = SeqFeature(FeatureLocation(start, end, strand=gene.strand),
                             type='domain')
            # Get the sequence
            seq = str(tmp.extract(record.seq))
            # Set the default properties
            gene.__upstream = seq.lower()
            gene.__message = "No RBS annotated, None found"

            # Try and do an automated shinefind call
            sds = sd_finder.list_sds(seq)
            if len(sds) > 0:
                sd = sds[0]
                gene.__upstream = sd_finder.highlight_sd(seq.lower(), sd['start'], sd['end'])
                gene.__message = "Unannotated but valid RBS"


            qc_features.append(gen_qc_feature(start, end, 'Missing RBS', strand=gene.strand, id_src=gene))

            bad += 1
            results.append(gene)
        else:
            if len(rbss) > 1:
                log.warn("%s RBSs found for gene %s", rbss[0].id, get_gff3_id(gene))
            any_rbss = True
            # get first RBS/CDS
            cds = list(genes(gene.sub_features, feature_type='CDS'))[0]
            rbs = rbss[0]

            # Get the distance between the two
            if gene.strand > 0:
                distance = cds.location.start - rbs.location.end
            else:
                distance = rbs.location.start - cds.location.end

            # If the RBS is too far away, annotate that
            if distance > lookahead_max:
                gene.__message = "RBS too far away (%s nt)" % distance

                qc_features.append(gen_qc_feature(
                    rbs.location.start,
                    rbs.location.end,
                    gene.__message,
                    strand=gene.strand,
                    id_src=gene,
                ))

                bad += 1
                results.append(gene)
            else:
                good += 1

    return good, bad, results, qc_features, any_rbss

# modified from get_orfs_or_cdss.py
# -----------------------------------------------------------


def require_sd(data, record, chrom_start, sd_min, sd_max):
    sd_finder = NaiveSDCaller()
    for putative_gene in data:
        if putative_gene[2] > 0:  # strand
            start = chrom_start + putative_gene[0] - sd_max
            end = chrom_start + putative_gene[0] - sd_min
        else:
            start = chrom_start + putative_gene[1] + sd_min
            end = chrom_start + putative_gene[1] + sd_max

        (start, end) = __ensure_location_in_bounds(start=start, end=end,
                                                    parent_length=record.__len__)
        tmp = SeqFeature(FeatureLocation(
            start, end, strand=putative_gene[2]), type='domain')
        # Get the sequence
        seq = str(tmp.extract(record.seq))
        sds = sd_finder.list_sds(seq)
        if len(sds) > 0:
            yield putative_gene + (start, end)


def annotation_table_report(record, wanted_cols):
    if wanted_cols is None or len(wanted_cols.strip()) == 0:
        return [], []

    def id(record, feature):
        """ID
        """
        return feature.id

    def name(record, feature):
        """Name
        """
        return feature.qualifiers.get('Name', ['None'])[0]

    def location(record, feature):
        """Location
        """
        return '{0.start}..{0.end}'.format(feature.location)

    def length(record, feature):
        """Length (AA)
        """
        cdss = list(genes(feature.sub_features, feature_type='CDS', sort=True))
        return str(sum([len(cds) for cds in cdss]) / 3)

    def notes(record, feature):
        """User entered Notes"""
        return feature.qualifiers.get('Note', [])

    def date_created(record, feature):
        """Created"""
        return feature.qualifiers.get('date_creation', ['None'])[0]

    def date_last_modified(record, feature):
        """Last Modified"""
        return feature.qualifiers.get('date_last_modified', ['None'])[0]

    def description(record, feature):
        """Description"""
        return feature.qualifiers.get('description', ['None'])[0]

    def owner(record, feature):
        """Owner

        User who created the feature. In a 464 scenario this may be one of
        the TAs."""
        return feature.qualifiers.get('owner', ['None'])[0]

    def product(record, feature):
        """Product

        User entered product qualifier (collects "Product" and "product"
        entries)"""
        return feature.qualifiers.get('product', feature.qualifiers.get('Product', ['None']))[0]

    def strand(record, feature):
        """Strand
        """
        return '+' if feature.location.strand > 0 else '-'

    def sd_spacing(record, feature):
        """Shine-Dalgarno spacing
        """
        rbss = get_rbs_from(gene)
        if len(rbss) == 0:
            return 'None'
        else:
            resp = []
            for rbs in rbss:
                cdss = list(genes(feature.sub_features, feature_type='CDS', sort=True))

                if rbs.location.strand > 0:
                    distance = min(cdss, key=lambda x: x.location.start - rbs.location.end)
                    distance_val = str(distance.location.start - rbs.location.end)
                    resp.append(distance_val)
                else:
                    distance = min(cdss, key=lambda x: x.location.end - rbs.location.start)
                    distance_val = str(rbs.location.start - distance.location.end)
                    resp.append(distance_val)

            if len(resp) == 1:
                return str(resp[0])
            return resp

    def sd_seq(record, feature):
        """Shine-Dalgarno sequence
        """
        rbss = get_rbs_from(gene)
        if len(rbss) == 0:
            return 'None'
        else:
            resp = []
            for rbs in rbss:
                resp.append(rbs.extract(record).seq)
            if len(resp) == 1:
                return str(resp[0])
            else:
                return resp

    def start_codon(record, feature):
        """Start Codon
        """
        cdss = list(genes(feature.sub_features, feature_type='CDS', sort=True))
        data = [x for x in cdss]
        if len(data) == 1:
            return str(data[0].extract(record).seq[0:3])
        else:
            return [
                '{0} ({1.location.start}..{1.location.end}:{1.location.strand})'.format(
                    x.extract(record).seq[0:3], x
                )
                for x in data
            ]

    def stop_codon(record, feature):
        """Stop Codon
        """
        return str(feature.extract(record).seq[-3:])

    def dbxrefs(record, feature):
        """DBxrefs
        """
        return feature.qualifiers.get('Dbxref', [])

    sorted_features = list(genes(record.features, sort=True))
    def upstream_feature(record, feature):
        """Next feature upstream"""
        upstream = None
        if feature.strand > 0:
            upstream_features = [x for x in sorted_features
                    if x.location.start < feature.location.start]
            if len(upstream_features) > 0:
                return upstream_features[-1]
            else:
                return None
        else:
            upstream_features = [x for x in sorted_features
                    if x.location.end > feature.location.end]

            if len(upstream_features) > 0:
                return upstream_features[0]
            else:
                return None

    def up_feat(record, feature):
        """Next feature upstream"""
        up = upstream_feature(record, feature)
        if up:
            return str(up)
        return 'None'


    def ig_dist(record, feature):
        """Distance to next feature on same strand"""
        up = upstream_feature(record, feature)
        if up:
            dist = None
            if feature.strand > 0:
                dist = feature.location.start - up.location.end
            else:
                dist = up.location.start - feature.location.end
            return str(dist)
        else:
            return 'None'


    cols = []
    data = []
    funcs = []
    lcl = locals()
    for x in [y.strip().lower() for y in wanted_cols.split(',')]:
        if x in lcl:
            funcs.append(lcl[x])
            # Keep track of docs
            func_doc = lcl[x].__doc__.strip().split('\n\n')
            # If there's a double newline, assume following text is the
            # "help" and the first part is the "name". Generate empty help
            # if not provided
            if len(func_doc) == 1:
                func_doc += ['']
            cols.append(func_doc)
        elif '__' in x:
           chosen_funcs = [lcl[y] for y in x.split('__')]
           func_doc = [' of '.join([y.__doc__.strip().split('\n\n')[0] for y in chosen_funcs[::-1]])]
           cols.append(func_doc)
           funcs.append(chosen_funcs)


    for gene in genes(record.features, sort=True):
        row = []
        for func in funcs:
            if isinstance(func, list):
                # If we have a list of functions, repeatedly apply them
                value = gene
                for f in func:
                    if value is None:
                        value = 'None'
                        break

                    value = f(record, value)
            else:
                # Otherwise just apply the lone function
                value = func(record, gene)

            if isinstance(value, list):
                value = [x.decode('utf-8') for x in value]
            else:
                value = value.decode('utf-8')

            row.append(value)
        # print row
        data.append(row)

    return data, cols


def excessive_gap(record, excess=50, excess_divergent=200, min_gene=30, slop=30, lookahead_min=5, lookahead_max=15):
    """
    Identify excessive gaps between gene features.

    Default "excessive" gap size is 10, but that should likely be larger.
    """
    results = []
    good = 0
    bad = 0

    contiguous_regions = []

    sorted_genes = sorted(genes(record.features), key=lambda feature: feature.location.start)
    if len(sorted_genes) == 0:
        log.warn("NO GENES FOUND")
        return good, bad, results, []

    current_gene = [
        int(sorted_genes[0].location.start),
        int(sorted_genes[0].location.end)
    ]
    for gene in sorted_genes[1:]:
        # If the gene's start is contiguous to the "current_gene", then we
        # extend current_gene
        if gene.location.start <= current_gene[1] + excess:
            current_gene[1] = int(gene.location.end)
        else:
            # If it's discontiguous, we append the region and clear.
            contiguous_regions.append(current_gene)
            current_gene = [int(gene.location.start), int(gene.location.end)]

    # This generally expected that annotations would NOT continue unto the end
    # of the genome, however that's a bug, and we can make it here with an
    # empty contiguous_regions list
    contiguous_regions.append(current_gene)

    for i in range(len(contiguous_regions) + 1):
        if i == 0:
            a = (1, 1)
            b = contiguous_regions[i]
        elif i >= len(contiguous_regions):
            a = contiguous_regions[i - 1]
            b = (len(record.seq), None)
        else:
            a = contiguous_regions[i - 1]
            b = contiguous_regions[i]

        gap_size = abs(b[0] - a[1])
        if gap_size > min(excess, excess_divergent):
            a_feat_l = itertools.islice(feature_lambda(sorted_genes, feature_test_location, {'loc': a[1]}, subfeatures=False), 1)
            b_feat_l = itertools.islice(feature_lambda(sorted_genes, feature_test_location, {'loc': b[0]}, subfeatures=False), 1)

            try:
                a_feat = next(a_feat_l)
            except StopIteration:
                # Triggers on end of genome
                a_feat = None
            try:
                b_feat = next(b_feat_l)
            except StopIteration:
                # Triggers on end of genome
                b_feat = None

            result_obj = [
                a[1],
                b[0],
                None if not a_feat else a_feat.location.strand,
                None if not b_feat else b_feat.location.strand
            ]

            if a_feat is None or b_feat is None:
                if gap_size > excess_divergent:
                    results.append(result_obj)
            else:
                if a_feat.location.strand == b_feat.location.strand and gap_size > excess:
                    results.append(result_obj)
                elif a_feat.location.strand != b_feat.location.strand and gap_size > excess_divergent:
                    results.append(result_obj)

    better_results = []
    qc_features = []
    of = OrfFinder(11, 'CDS', 'closed', min_gene)

    for result_obj in results:
        start = result_obj[0]
        end = result_obj[1]
        f = gen_qc_feature(start, end, 'Excessive gap, %s bases' % abs(end-start))
        qc_features.append(f)
        putative_genes = of.putative_genes_in_sequence(str(record[start - slop:end + slop].seq))
        putative_genes = list(require_sd(putative_genes, record, start, lookahead_min, lookahead_max))
        for putative_gene in putative_genes:
            # (0, 33, 1, 'ATTATTTTATCAAAACGCTTTACAATCTTTTAG', 'MILSKRFTIF', 123123, 124324)
            possible_gene_start = start + putative_gene[0]
            possible_gene_end = start + putative_gene[1]

            possible_cds = SeqFeature(
                FeatureLocation(
                    possible_gene_start, possible_gene_end,
                    strand=putative_gene[2],
                ),
                type='CDS'
            )

            # Now we adjust our boundaries for the RBS that's required
            # There are only two cases, the rbs is upstream of it, or downstream
            if putative_gene[5] < possible_gene_start:
                possible_gene_start = putative_gene[5]
            else:
                possible_gene_end = putative_gene[6]

            possible_rbs = SeqFeature(
                FeatureLocation(
                    putative_gene[5], putative_gene[6],
                    strand=putative_gene[2],
                ),
                type='Shine_Dalgarno_sequence'
            )

            possible_gene = SeqFeature(
                FeatureLocation(
                    possible_gene_start, possible_gene_end,
                    strand=putative_gene[2],
                ),
                type='gene',
                qualifiers={
                    'note': ['Possible gene']
                }
            )
            possible_gene.sub_features = [possible_rbs, possible_cds]
            qc_features.append(possible_gene)

        better_results.append(result_obj + [len(putative_genes)])

    # Bad gaps are those with more than zero possible genes found
    bad = len([x for x in better_results if x[2] > 0])
    # Generally taking "good" here as every possible gap in the genome
    # Thus, good is TOTAL - gaps
    good = len(sorted_genes) + 1 - bad
    # and bad is just gaps
    return good, bad, better_results, qc_features

def phi(x):
    """Standard phi function used in calculation of normal distribution"""
    return math.exp(-1 * math.pi * x * x)

def norm(x, mean=0, sd=1):
    """
    Normal distribution. Given an x position, a mean, and a standard
    deviation, calculate the "y" value. Useful for score scaling

    Modified to multiply by SD. This means even at sd=5, norm(x, mean) where x = mean => 1, rather than 1/5.
    """
    return (1 / float(sd)) * phi(float(x - mean) / float(sd)) * sd

def coding_density(record, mean=92.5, sd=20):
    """
    Find coding density in the genome
    """
    feature_lengths = 0

    for gene_a in coding_genes(record.features):
        feature_lengths += sum([
            len(x) for x in
            genes(gene_a.sub_features, feature_type='CDS')
        ])

    avgFeatLen = float(feature_lengths) / float(len(record.seq))
    return int(norm(100 * avgFeatLen, mean=mean, sd=sd) * 100), int(100 * avgFeatLen)


def excessive_overlap(record, excessive=15):
    """
    Find excessive overlaps in the genome, where excessive is defined as 15
    bases.

    Does a product of all the top-level features in the genome, and calculates
    gaps.
    """
    results = []
    bad = 0
    qc_features = []

    for (gene_a, gene_b) in itertools.combinations(coding_genes(record.features), 2):
        # Get the CDS from the subfeature list.
        # TODO: not recursive.
        cds_a = [x for x in genes(gene_a.sub_features, feature_type='CDS')]
        cds_b = [x for x in genes(gene_b.sub_features, feature_type='CDS')]

        if len(cds_a) == 0:
            log.warn("Gene missing subfeatures; %s", get_gff3_id(gene_a))
            continue

        if len(cds_b) == 0:
            log.warn("Gene missing subfeatures; %s", get_gff3_id(gene_b))
            continue

        cds_a = cds_a[0]
        cds_b = cds_b[0]

        # Set of locations that are included in the CDS of A and the
        # CDS of B
        cas = set(range(cds_a.location.start, cds_a.location.end))
        cbs = set(range(cds_b.location.start, cds_b.location.end))

        # Here we calculate the intersection between the two sets, and
        # if it's larger than our excessive size, we know that they're
        # overlapped
        ix = cas.intersection(cbs)
        if len(ix) >= excessive:
            bad += float(len(ix)) / float(excessive)
            qc_features.append(gen_qc_feature(
                min(ix),
                max(ix),
                "Excessive Overlap",
                id_src=gene_a
            ))
            results.append((gene_a, gene_b, min(ix), max(ix)))

    # Good isn't accurate here. It's a triangle number and just ugly, but we
    # don't care enough to fix it.
    good = len(list(coding_genes(record.features)))
    good = int(good - bad)
    if good < 0:
        good = 0
    return good, int(bad), results, qc_features


def get_encouragement(score):
    """Some text telling the user how they did
    """
    for encouragement in ENCOURAGEMENT:
        if score > encouragement[0]:
            return encouragement[1]
    return ENCOURAGEMENT[-1][1]


def find_morons(record):
    """Locate morons in the genome

    Don't even know why...

    TODO: remove? Idk.
    """
    results = []
    good = 0
    bad = 0

    gene_features = list(coding_genes(record.features))
    for i, gene in enumerate(gene_features):
        two_left = gene_features[i - 2:i]
        two_right = gene_features[i + 1:i + 1 + 2]
        strands = [x.strand for x in two_left] + [x.strand for x in two_right]
        anticon = [x for x in strands if x != gene.strand]

        if len(anticon) == 4:
            has_rbs = [x.type == "Shine_Dalgarno_sequence" for x in
                       gene.sub_features]
            if any(has_rbs):
                rbs = [x for x in gene.sub_features if x.type ==
                       "Shine_Dalgarno_sequence"][0]
                rbs_msg = str(rbs.extract(record.seq))
            else:
                rbs_msg = "No RBS Available"
            results.append((gene, two_left, two_right, rbs_msg))
            bad += 1
        else:
            good += 1
    return good, bad, results, []


def bad_gene_model(record):
    """Find features without product
    """
    results = []
    good = 0
    qc_features = []

    for gene in coding_genes(record.features):
        exons = [x for x in genes(gene.sub_features, feature_type='exon') if len(x) > 10]
        CDSs = [x for x in genes(gene.sub_features, feature_type='CDS')]

        if len(exons) == 1 and len(CDSs) == 1:
            exon = exons[0]
            CDS = CDSs[0]
            if len(exon) != len(CDS):
                results.append((
                    get_gff3_id(gene),
                    exon,
                    CDS,
                    'CDS does not extend to full length of gene',
                ))
                qc_features.append(gen_qc_feature(
                    exon.location.start, exon.location.end,
                    'CDS does not extend to full length of gene',
                    strand=exon.strand,
                    id_src=gene
                ))
            else:
                good += 1
        else:
            log.warn("Could not handle %s, %s", exons, CDSs)
            results.append((
                get_gff3_id(gene),
                None,
                None,
                '{0} exons, {1} CDSs'.format(len(exons), len(CDSs))
            ))

    return good, len(results), results, qc_features


def weird_starts(record):
    """Find features without product
    """
    good = 0
    bad = 0
    qc_features = []

    overall = {}
    for gene in coding_genes(record.features):
        seq = [x for x in genes(gene.sub_features, feature_type='CDS')]
        if len(seq) == 0:
            log.warn("No CDS for gene %s", get_gff3_id(gene))
            continue
        else:
            seq = seq[0]

        seq_str = str(seq.extract(record.seq))
        start_codon = seq_str[0:3]
        stop_codon = seq_str[-3]
        seq.__start = start_codon
        seq.__stop = stop_codon
        if start_codon not in overall:
            overall[start_codon] = 1
        else:
            overall[start_codon] += 1

        if start_codon not in ('ATG', 'TTG', 'GTG'):
            log.warn("Weird start codon (%s) on %s", start_codon, get_gff3_id(gene))
            seq.__error = 'Unusual start codon %s' % start_codon

            s = 0
            e = 0
            if seq.strand > 0:
                s = seq.location.start
                e = seq.location.start + 3
            else:
                s = seq.location.end
                e = seq.location.end - 3

            qc_features.append(gen_qc_feature(
                s, e,
                'Weird start codon',
                strand=seq.strand,
                id_src=gene
            ))
            bad += 1
        else:
            good += 1

    return good, bad, qc_features, qc_features, overall


def missing_tags(record):
    """Find features without product
    """
    results = []
    good = 0
    bad = 0
    qc_features = []

    for gene in coding_genes(record.features):
        cds = [x for x in genes(gene.sub_features, feature_type='CDS')]
        if len(cds) == 0:
            log.warn("Gene missing CDS subfeature %s", get_gff3_id(gene))
            continue

        cds = cds[0]

        if 'product' not in cds.qualifiers:
            log.info("Missing product tag on %s", get_gff3_id(gene))
            qc_features.append(gen_qc_feature(
                cds.location.start,
                cds.location.end,
                'Missing product tag',
                strand=cds.strand
            ))
            results.append(cds)
            bad += 1
        else:
            good += 1

    return good, bad, results, qc_features


def evaluate_and_report(annotations, genome, user_email, gff3=None,
                        tbl=None, sd_min=5, sd_max=15, gap_dist=45,
                        overlap_dist=15, min_gene_length=30,
                        excessive_gap_dist=50, excessive_gap_divergent_dist=200,
                        reportTemplateName='phage_annotation_validator.html',
                        annotationTableCols=''):
    """
    Generate our HTML evaluation of the genome
    """
    # Get features from GFF file
    seq_dict = SeqIO.to_dict(SeqIO.parse(genome, "fasta"))
    # Get the first GFF3 record
    # TODO: support multiple GFF3 files.
    record = list(GFF.parse(annotations, base_dict=seq_dict))[0]

    gff3_qc_record = SeqRecord(record.id, id=record.id)
    gff3_qc_record.features = []
    gff3_qc_features = []

    log.info("Locating missing RBSs")
    #mb_any = "did they annotate ANY rbss? if so, take off from score."
    mb_good, mb_bad, mb_results, mb_annotations, mb_any = missing_rbs(
        record,
        lookahead_min=sd_min,
        lookahead_max=sd_max
    )
    gff3_qc_features += mb_annotations

    log.info("Locating excessive gaps")
    eg_good, eg_bad, eg_results, eg_annotations = excessive_gap(
        record,
        excess=excessive_gap_dist,
        excess_divergent=excessive_gap_divergent_dist,
        min_gene=min_gene_length,
        slop=overlap_dist,
        lookahead_min=sd_min,
        lookahead_max=sd_max
    )
    gff3_qc_features += eg_annotations

    log.info("Locating excessive overlaps")
    eo_good, eo_bad, eo_results, eo_annotations = excessive_overlap(record, excessive=overlap_dist)
    gff3_qc_features += eo_annotations

    log.info("Locating morons")
    mo_good, mo_bad, mo_results, mo_annotations = find_morons(record)
    gff3_qc_features += mo_annotations

    log.info("Locating missing tags")
    mt_good, mt_bad, mt_results, mt_annotations = missing_tags(record)
    gff3_qc_features += mt_annotations

    log.info("Determining coding density")
    cd, cd_real = coding_density(record)

    log.info("Locating weird starts")
    ws_good, ws_bad, ws_results, ws_annotations, ws_overall = weird_starts(record)
    gff3_qc_features += ws_annotations

    log.info("Producing an annotation table")
    annotation_table_data, annotation_table_col_names = annotation_table_report(record, annotationTableCols)

    log.info("Locating bad gene models")
    gm_good, gm_bad, gm_results, gm_annotations = bad_gene_model(record)
    if gm_good + gm_bad == 0:
        gm_bad = 1

    good_scores = [eg_good, eo_good, mt_good, ws_good, gm_good]
    bad_scores = [eg_bad, eo_bad, mt_bad, ws_bad, gm_bad]

    # Only if they tried to annotate RBSs do we consider them.
    if mb_any:
        good_scores.append(mb_good)
        bad_scores.append(mb_bad)
    subscores = []

    for (g, b) in zip(good_scores, bad_scores):
        if g + b == 0:
            s = 0
        else:
            s = int(100 * float(g) / (float(b) + float(g)))
        subscores.append(s)
    subscores.append(cd)

    score = int(float(sum(subscores)) / float(len(subscores)))

    # This is data that will go into our HTML template
    kwargs = {
        'upstream_min': sd_min,
        'upstream_max': sd_max,
        'record_name': record.id,

        'score': score,
        'encouragement': get_encouragement(score),

        'rbss_annotated': mb_any,
        'missing_rbs': mb_results,
        'missing_rbs_good': mb_good,
        'missing_rbs_bad': mb_bad,
        'missing_rbs_score': (100 * mb_good / (mb_good + mb_bad)),

        'excessive_gap': eg_results,
        'excessive_gap_good': eg_good,
        'excessive_gap_bad': eg_bad,
        'excessive_gap_score': (100 * eo_good / (eo_good + eo_bad)),

        'excessive_overlap': eo_results,
        'excessive_overlap_good': eo_good,
        'excessive_overlap_bad': eo_bad,
        'excessive_overlap_score': (100 * eo_good / (eo_good + eo_bad)),

        'morons': mo_results,
        'morons_good': mo_good,
        'morons_bad': mo_bad,
        'morons_score': (100 * mo_good / (mo_good + mo_bad)),

        'missing_tags': mt_results,
        'missing_tags_good': mt_good,
        'missing_tags_bad': mt_bad,
        'missing_tags_score': (100 * mt_good / (mt_good + mt_bad)),

        'weird_starts': ws_results,
        'weird_starts_good': ws_good,
        'weird_starts_bad': ws_bad,
        'weird_starts_overall': ws_overall,
        'weird_starts_overall_sorted_keys': sorted(ws_overall, reverse=True, key=lambda x: ws_overall[x]),
        'weird_starts_score': (100 * ws_good / (ws_good + ws_bad)),

        'gene_model': gm_results,
        'gene_model_good': gm_good,
        'gene_model_bad': gm_bad,
        'gene_model_score': (100 * gm_good / (gm_good + gm_bad)),

        'coding_density': cd,
        'coding_density_real': cd_real,
        'coding_density_score': cd,

        'annotation_table_data': annotation_table_data,
        'annotation_table_col_names': annotation_table_col_names,
    }

    with open(tbl, 'w') as handle:
        kw_subset = {}
        for key in kwargs:
            if key in ('score', 'record_name') or '_good' in key or '_bad' in key or '_overall' in key:
                kw_subset[key] = kwargs[key]
        json.dump(kw_subset, handle)

    with open(gff3, 'w') as handle:
        gff3_qc_record.features = gff3_qc_features
        gff3_qc_record.annotations = {}
        GFF.write([gff3_qc_record], handle)

    def nice_strand(direction):
        if direction > 0:
            return '→'.decode('utf-8')
        else:
            return '←'.decode('utf-8')

    env = Environment(loader=FileSystemLoader(SCRIPT_PATH), trim_blocks=True, lstrip_blocks=True)
    env.filters['nice_id'] = get_gff3_id
    env.filters['nice_strand'] = nice_strand
    tpl = env.get_template(reportTemplateName)
    return tpl.render(**kwargs).encode('utf-8')


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description='rebase gff3 features against parent locations', epilog="")
    parser.add_argument('annotations', type=file, help='Parent GFF3 annotations')
    parser.add_argument('genome', type=file, help='Genome Sequence')
    parser.add_argument('--gff3', type=str, help='GFF3 Annotations', default='qc_annotations.gff3')
    parser.add_argument('--tbl', type=str, help='Table for noninteractive parsing', default='qc_results.json')

    parser.add_argument('--sd_min', type=int, help='Minimum distance from gene start for an SD to be', default=5)
    parser.add_argument('--sd_max', type=int, help='Maximum distance from gene start for an SD to be', default=15)

    parser.add_argument('--gap_dist', type=int, help='Maximum distance from gene start for an SD to be', default=30)
    parser.add_argument('--overlap_dist', type=int, help='Maximum distance from gene start for an SD to be', default=30)

    parser.add_argument('--min_gene_length', type=int, help='Minimum length for a putative gene call (AAs)', default=30)

    parser.add_argument('--excessive_gap_dist', type=int, help='Maximum distance between two genes', default=40)
    parser.add_argument('--excessive_gap_divergent_dist', type=int, help='Maximum distance between two divergent genes', default=200)

    parser.add_argument('--reportTemplateName', help='Report template file name', default='phageqc_report_full.html')
    parser.add_argument('--user_email')

    parser.add_argument('--annotationTableCols', help='Select columns to report in the annotation table output format')

    args = parser.parse_args()

    print evaluate_and_report(**vars(args))
    # evaluate_and_report(**vars(args))