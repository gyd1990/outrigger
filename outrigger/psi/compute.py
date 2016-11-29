import logging

import joblib
import pandas as pd

from ..common import ILLEGAL_JUNCTIONS, MIN_READS, READS, UNEVEN_COVERAGE_MULTIPLIER
from ..util import progress, done


logging.basicConfig()

idx = pd.IndexSlice


def _scale(x, n_junctions, method='mean', min_reads=MIN_READS):
    # multiplier = -1 if (x < min_reads).any() else 1
    if method == 'mean':
        return x.sum()/float(n_junctions)
    elif method == 'min':
        return x.min()


def _filter_and_scale(reads, n_junctions, debug=False, min_reads=MIN_READS,
                      method='mean'):
    """Remove samples without reads on all junctions and sum across junctions

    If any junction on the isoform has fewer than the minimum reads, flag it
    as -1

    Parameters
    ----------

    method : 'mean' | 'min'
        If there are more than 2 junctions for this isoform, then they have to
        be consolidated somehow.
        - "mean": Sum the reads and divide by the number of junctions
        - "min": Use the minimum number of reads
    """
    logger = logging.getLogger('outrigger.psi.filter_and_scale')
    if debug:
        logger.setLevel(10)

    if reads.empty:
        return reads

    if n_junctions > 1:
        # Remove all samples that don't have all required junctions
        reads = reads.groupby(level=1).filter(
            lambda x: len(x) == n_junctions)
        logger.debug('filtered reads:\n' + repr(reads.head()))

    reads = reads.groupby(level=1).apply(
        lambda x: _scale(x, n_junctions, method, min_reads))

    # Sum all reads from junctions in the same samples (level=1), remove NAs
    # reads = reads.groupby(level=1).sum().dropna()
    logger.debug('summed reads:\n' + repr(reads.head()))

    return reads


def _maybe_get_isoform_reads(splice_junction_reads, junction_locations,
                             isoform_junctions, reads_col,
                             min_reads=MIN_READS):
    """If there are junction reads at a junction_locations, return the reads

    Parameters
    ----------
    splice_junction_reads : pandas.DataFrame
        A table of the number of junction reads observed in each sample.
        *Important:* This must be multi-indexed by the junction and sample id
    junction_locations : pandas.Series
        Mapping of junction names (from var:`isoform_junctions`) to their
        chromosomal locations
    isoform_junctions : list of str
        Names of the isoform_junctions in question, e.g.
        ['junction12', 'junction13']
    reads_col : str
        Name of the column containing the number of reads in
        `splice_junction_reads`

    Returns
    -------
    reads : pandas.Series
        Number of reads at each junction for this isoform
    """
    junctions = junction_locations[isoform_junctions]
    junction_names = splice_junction_reads.index.levels[0]

    # Get junctions that can't exist for this to be a valid event
    illegal_junctions = junction_locations[ILLEGAL_JUNCTIONS]
    illegal_samples = []
    if isinstance(illegal_junctions, str):
        illegal_junctions = illegal_junctions.split('|')

        # Get samples that have illegal junctions
        if junction_names.isin(illegal_junctions).sum() > 0:
            illegal_reads = splice_junction_reads.loc[
                idx[illegal_junctions, :], reads_col]
            illegal_samples = illegal_reads.index.get_level_values('sample_id')

    if junction_names.isin(junctions).sum() > 0:
        import pdb; pdb.set_trace()
        reads_subset = splice_junction_reads.loc[idx[junctions, :], reads_col]
        # legal_samples = reads_subset.index.get_level_values('sample_id')
        # legal_samples = legal_samples.difference(illegal_samples)
        # reads_df = reads_subset.loc[idx[:, legal_samples]].unstack(level=0)
        reads_df = reads_subset.unstack(level=0)
        reads_df = reads_subset.fillna(0)
        return reads_df
    else:
        return pd.Series()


def _remove_insufficient_reads(isoform1, isoform2):
    """Exclude samples whose junctions have insufficient reads (marked with -1)

    In filter_and_sum, if any junction had fewer than expected reads, set it
     to -1

    ONLY Use junctions with individually sufficient reads
    """
    isoform1, isoform2 = isoform1.align(isoform2, 'outer')

    isoform1 = isoform1.fillna(0)
    isoform2 = isoform2.fillna(0)

    invalid = (isoform1 < 0) | (isoform2 < 0)

    isoform1 = isoform1[~invalid]
    isoform2 = isoform2[~invalid]
    return isoform1, isoform2


def _single_sample_maybe_sufficient_reads(isoform1, isoform2, n_junctions,
                                          min_reads, case letters='ab'):
    """Check if the sum of reads is enough compared to number of junctions

    Parameters
    ----------
    isoform1, isoform2 : pandas.Series
        Number of reads found for a single sample's isoform1 and isoform2
        junctions
    n_junctions : int
        Total number of junctions, also len(isoform1) + len(isoform2)
    min_reads : int
        Minimum number of reads per junction
    case : str
        English explanation with case number for why an event was rejected or
        not
    letters : str, optional
        Cases could have multiple clauses, by default the case is "option a" or
         "option b" but other letters could be used. (default='ab')

    Returns
    -------

    """
    if (isoform1.sum() + isoform2.sum()) >= (min_reads * n_junctions):
        # Case 5a: There are sufficient junction reads
        return isoform1, isoform2, '{case}, option {letter}: There are ' \
                                   'sufficient junction ' \
                                   'reads'.format(case=case, letter=letters[0])
    else:
        # Case 5b: There are insufficient junction reads
        return None, None, '{case}, option {letter}: There are insufficient ' \
                           'junction reads'.format(case=case,
                                                   letter=letters[1])


def _single_sample_check_unequal_read_coverage(isoform, multiplier=UNEVEN_COVERAGE_MULTIPLIER):
    """If one junction of an isoform is more heavily covered, reject it

    If the difference in read depth between two junctions of an isoform is
    higher than a multiplicative amount, reject the isoform.

    If the isoform only has one junction, don't reject it.

    Possible fallacy: assumes there can be at most two junctions per isoform...

    Parameters
    ----------
    isoform : pandas.Series
        Number of exon-exon junction-spanning reads found for an isoform
    multiplier : int
        Scale factor for the maximum amount bigger one side of a junction can
        be before rejecting the event, e.g. for an SE event with two junctions,
        junction12 and junction23, junction12=40 but junction23=500, then this
        event would be rejected because 500 > 40*10

    Returns
    -------
    isoform : pandas.Series or None
        If not rejected, the original is returned, otherwise return None
    """

    if len(isoform) == 1:
        return isoform

    multiplied = isoform * multiplier

    junction0 = isoform.iloc[0]
    junction1 = isoform.iloc[1]

    if junction0 > junction1 and junction0 > multiplied.iloc[1]:
        return None
    elif junction1 > junction0 and junction1 > multiplied.iloc[0]:
        return None
    else:
        return isoform


def _maybe_reject(reads, isoform1_ids, isoform2_ids, illegal_ids,
                  n_junctions, min_reads=MIN_READS,
                  uneven_coverage_multiplier=UNEVEN_COVERAGE_MULTIPLIER):
    """Remove samples with reads that are incompatible with event definition

    Parameters
    ----------
    reads : pandas.DataFrame
        A (n_samples, n_junctions) table of the number of reads found in each
        samples' exon-exon junctions
    isoform1_ids : list of str
        Column names in ``reads`` tha correspond to the junction ids that are
        contained within isoform 1
    isoform2_ids :
        Column names in ``reads`` tha correspond to the junction ids that are
        contained within isoform 2
    illegal_ids : list of str
        Column names in ``reads`` tha correspond to the junction ids that are
        contained within junctions that are not compatible with the event
        definition
    n_junctions : int
        Total number of legal junctions,
        i.e. len(isoform1_ids) + len(isoform2_ids)
    min_reads : int
        Minimum number of reads for a junction to be viable. The rules
        governing compatibility of events are complex, and it is recommended to
        read the documentation for ``outrigger psi``
    uneven_coverage_multiplier : int

    Returns
    -------

    """
    if not pd.isnull(illegal_ids):
        samples_with_illegal_coverage = reads[illegal_ids] >= min_reads
        reads = reads.loc[~samples_with_illegal_coverage]

    # import pdb; pdb.set_trace()
    maybe_rejected = zip(*reads.apply(
        lambda sample: _single_maybe_reject(
            sample, isoform1_ids, isoform2_ids,
            n_junctions=n_junctions, min_reads=min_reads,

            uneven_coverage_multiplier=uneven_coverage_multiplier), axis=1))
    return maybe_rejected


def _single_maybe_reject(sample, isoform1_ids, isoform2_ids,
                         n_junctions, min_reads=MIN_READS,
                         uneven_coverage_multiplier=UNEVEN_COVERAGE_MULTIPLIER):
    """Given a row of junction reads, return a filtered row of reads

    For a single sample's junction reads of an isoform, check if they should be
    rejected, and if they are, return a row with all NAs for the reads. Always
    include the case by which the reads were or were not rejected

    Parameters
    ----------
    sample : pandas.Series
        A single sample's junction reads across all isoforms of an alternative
        event
    isoform1_ids : list
        List of strings that correspond to indicies in ``sample``
    isoform2_ids : list
        List of strings that correspond to indicies in ``sample``
    n_junctions : int
        Total number of junctions expected in the splicing event
    min_reads : int, optional
        Minimum number of junction reads for an event to be valid. See
        documentation for much more detailed information regarding when events
        are rejected or retained. (default=10)
    uneven_coverage_multiplier : int, optional
        When checking for uneven coverage between two sides of a junction, one
        side must be this amount bigger than the other side to be rejected.
        For example, if one side has 10x (default) more read coverage than the
        other, then reject the event. (default=10)

    Returns
    -------
    single_maybe_rejected : pandas.Series
        Unrejected reads of a single samples' splicing event. If the event was
        rejected, the reads are replaced with NAs. This series has one more
        field than the input "sample", with the field of "Case" that explains
        why this event was or was not rejected.
    """
    isoform1, isoform2, case = _single_isoform_maybe_reject(
            sample[isoform1_ids], sample[isoform2_ids],
            n_junctions=n_junctions, min_reads=min_reads,
            uneven_coverage_multiplier=uneven_coverage_multiplier)
    if isoform1 is None:
        single_maybe_rejected = pd.Series(None, index=sample.index)
    else:
        single_maybe_rejected = sample
    single_maybe_rejected['Case'] = case
    return single_maybe_rejected



def _single_isoform_maybe_reject(
    isoform1, isoform2, n_junctions, min_reads=MIN_READS,
    uneven_coverage_multiplier=UNEVEN_COVERAGE_MULTIPLIER):
    """Given junction reads of isoform1 and isoform2, remove if they are bad

    Parameters
    ----------
    isoform1, isoform2 : pandas.Series
        Number of reads found on exon-exon junctions for isoform1 and isoform2
    n_junctions : int
        Total number of junctions. Could also be found by
        len(isoform1) + len(isoform2)
    min_reads : int, optional
        Minimum number of reads for a junction to be counted, though the full
        explanation is a little more complicated, please see the documentation
        for more details. (default=10)
    uneven_coverage_multiplier : int, optional
        Scale factor for the maximum amount bigger one side of a junction can
        be before rejecting the event, e.g. for an SE event with two junctions,
        junction12 and junction23, junction12=40 but junction23=500, then this
        event would be rejected because 500 > 40*10

    Returns
    -------
    isoform1, isoform2 : pandas.Series or None
        If the event was not rejected, return the original event, otherwise
        return None
    case : str
        Reason for rejecting or retaining the event
    """

    isoform1 = _single_sample_check_unequal_read_coverage(isoform1, uneven_coverage_multiplier)
    isoform2 = _single_sample_check_unequal_read_coverage(isoform2, uneven_coverage_multiplier)

    if isoform1 is None or isoform2 is None:
        return None, None, "Case 0: Unequal read coverage"

    if (isoform1 >= min_reads).all() and (isoform2 == 0).all():
        # Case 1: Perfect exclusion
        return isoform1, isoform2, 'Case 1: Perfect exclusion'
    elif (isoform1 == 0).all() and (isoform2 >= min_reads).all():
        # Case 2: Perfect inclusion
        return isoform1, isoform2, 'Case 2: Perfect inclusion'
    elif (isoform1 >= min_reads).all() and (isoform2 >= min_reads).all():
        # Case 3: Sufficient coverage on both isoforms
        return isoform1, isoform2, 'Case 3: Sufficient coverage on both ' \
                                   'isoforms'
    elif (isoform1 == 0).any() or (isoform2 == 0).any():
        # Case 4: Any observed junction is zero and it's not all of one isoform
        return None, None, "Case 4: Any observed junction is zero and it's " \
                           "not all of one isoform"
    elif (isoform1 >= min_reads).all() and (isoform2 < min_reads).all():
        # Case 5: Isoform1 totally covered and isoform2 not
        return _single_sample_maybe_sufficient_reads(isoform1, isoform2, n_junctions,
                                                     min_reads, 'Case 5: Isoform1 totally '
                                                  'covered and isoform2 not')
    elif (isoform1 < min_reads).all() and (isoform2 >= min_reads).all():
        # Case 6: Isoform2 is totally covered and isoform1 is not
        return _single_sample_maybe_sufficient_reads(isoform1, isoform2, n_junctions,
                                                     min_reads, 'Case 6: Isoform2 is '
                                                  'totally covered and '
                                                  'isoform1 is not')
    elif (isoform1 >= min_reads).all() and (isoform2 < min_reads).any():
        # Case 7: Isoform 1 is fully covered and isoform2 is questionable
        return _single_sample_maybe_sufficient_reads(isoform1, isoform2, n_junctions,
                                                     min_reads, 'Case 7: Isoform 1 is fully '
                                                  'covered and isoform2 is '
                                                  'questionable')
    elif (isoform1 < min_reads).any() and (isoform2 >= min_reads).all():
        # Case 8: Isoform 1 is fully covered and isoform2 is questionable
        return _single_sample_maybe_sufficient_reads(isoform1, isoform2, n_junctions,
                                                     min_reads, 'Case 8: Isoform 1 is fully '
                                                  'covered and isoform2 is '
                                                  'questionable')
    elif (isoform1 < min_reads).any() or (isoform2 < min_reads).any():
        # Case 9: insufficient reads somehow
        if (isoform1 < min_reads).all() and (isoform2 < min_reads).any():
            # Case 9a: 3 junctions have less than minimum reads (2 on iso1
            # and 1 on iso2)
            return None, None, 'Case 9a: 3 junctions have less than minimum ' \
                               'reads (2 on iso1 and 1 on iso2)'
        if (isoform1 < min_reads).any() and (isoform2 < min_reads).all():
            # Case 9b: 3 junctions have less than minimum reads (2 on iso2
            # and one on iso1)
            return None, None, 'Case 9b: 3 junctions have less than minimum ' \
                               'reads (2 on iso2 and one on iso1)'

        return _single_sample_maybe_sufficient_reads(isoform1, isoform2, n_junctions,
                                                     min_reads, case='Case 9: Insufficient '
                                                       'reads somehow',
                                                     letters='cd')

    elif (isoform1 < min_reads).any() or (isoform2 < min_reads).any():
        # Case 10: isoform1 and isoform2 don't have sufficient reads
        return None, None, "Case 10: isoform1 and isoform2 don't have " \
                           "sufficient reads"

    # If none of these is true, then there's some uncaught case
    return '???', '???', "Case ???"


def _single_event_psi(event_id, event_df, junction_reads_2d,
                      isoform1_junction_numbers, isoform2_junction_numbers,
                      min_reads=MIN_READS, method='mean',
                      multiplier=UNEVEN_COVERAGE_MULTIPLIER, debug=False, log=None):
    """Calculate percent spliced in for a single event across all samples

    Returns
    -------
    psi : pandas.Series or None
        If unable to calculate psi for this event due to insufficient junctions
        then None is returned.
    """
    junction_locations = event_df.iloc[0]

    n_junctions1 = len(isoform1_junction_numbers)
    n_junctions2 = len(isoform2_junction_numbers)
    n_junctions = n_junctions1 + n_junctions2

    isoform1_junction_ids = junction_locations[isoform1_junction_numbers].tolist()
    isoform2_junction_ids = junction_locations[isoform2_junction_numbers].tolist()
    illegal_junction_ids = junction_locations[ILLEGAL_JUNCTIONS].tolist()

    junction_cols = isoform1_junction_ids + isoform2_junction_ids

    if not isinstance(illegal_junction_ids, float):
        junction_cols += illegal_junction_ids

    reads = junction_reads_2d[junction_cols]

    maybe_rejected = _maybe_reject(
        reads, isoform1_junction_ids, isoform2_junction_ids,
        illegal_junction_ids, n_junctions, min_reads=min_reads,
        uneven_coverage_multiplier=multiplier)

    import pdb; pdb.set_trace()
    return
    if debug and log is not None:
        junction_cols = isoform1_junction_numbers + isoform2_junction_numbers
        log.debug('--- junction columns of event ---\n%s',
                  repr(junction_locations[junction_cols]))
        log.debug('--- isoform1 ---\n%s', repr(isoform1))
        log.debug('--- isoform2 ---\n%s', repr(isoform2))

    isoform1 = _filter_and_scale(isoform1, n_junctions1, min_reads=min_reads,
                                 method=method)
    isoform2 = _filter_and_scale(isoform2, n_junctions2, min_reads=min_reads,
                                 method=method)

    if isoform1.empty and isoform2.empty:
        # If both are empty after filtering this event --> don't calculate
        return

    if debug and log is not None:
        log.debug('\n- After filter and sum -')
        log.debug('--- isoform1 ---\n%s', repr(isoform1))
        log.debug('--- isoform2 ---\n%s', repr(isoform2))

    isoform1, isoform2 = _remove_insufficient_reads(isoform1, isoform2)

    if debug and log is not None:
        log.debug('\n- After removing insufficient reads -')
        log.debug('--- isoform1 ---\n%s', repr(isoform1))
        log.debug('--- isoform2 ---\n%s', repr(isoform2))

    psi = isoform2 / (isoform2 + isoform1)
    psi.name = event_id

    if debug and log is not None:
        log.debug('--- Psi ---\n%s', repr(psi))

    if not psi.empty:
        return psi, isoform1, isoform2


def _maybe_parallelize_psi(event_annotation, junction_reads_2d,
                           isoform1_junctions, isoform2_junctions,
                           reads_col=READS, min_reads=MIN_READS, method='mean',
                           multiplier=UNEVEN_COVERAGE_MULTIPLIER, n_jobs=-1,
                           debug=False, log=None):
    # There are multiple rows with the same event id because the junctions
    # are the same, but the flanking exons may be a little wider or shorter,
    # but ultimately the event Psi is calculated only on the junctions so the
    # flanking exons don't matter for this. But, all the exons are in
    # exon\d.bed in the index! And you, the lovely user, can decide what you
    # want to do with them!
    grouped = event_annotation.groupby(level=0, axis=0)

    n_events = len(grouped.size())

    if n_jobs == 1:
        progress('\tIterating over {} events ...\n'.format(n_events))
        psis = []
        isoform1s = []
        isoform2s = []
        for event_id, event_df in grouped:
            outputs = _single_event_psi(
                event_id, event_df, junction_reads_2d,
                isoform1_junctions, isoform2_junctions,
                min_reads=min_reads, multiplier=multiplier,
                method=method, debug=debug, log=log)
            # psis.append(psi)
            # isoform1s.append(isoform1)
            # isoform2s.append(isoform2)
    else:
        processors = n_jobs if n_jobs > 0 else joblib.cpu_count()
        progress("\tParallelizing {} events' Psi calculation across {} "
                 "CPUs ...\n".format(n_events, processors))
        outputs = joblib.Parallel(n_jobs=n_jobs)(
            joblib.delayed(_single_event_psi)(
                event_id, event_df, junction_reads_2d,
                isoform1_junctions, isoform2_junctions, reads_col,
                min_reads=min_reads, multiplier=multiplier, method=method)
            for event_id, event_df in grouped)
        import pdb; pdb.set_trace()

    return psis


def calculate_psi(event_annotation, junction_reads_2d,
                  isoform1_junctions, isoform2_junctions, reads_col=READS,
                  min_reads=MIN_READS, method='mean',
                  multiplier=UNEVEN_COVERAGE_MULTIPLIER,
                  n_jobs=-1, debug=False):
    """Compute percent-spliced-in of events based on junction reads

    Parameters
    ----------
    event_annotation : pandas.DataFrame
        A table where each row represents a single splicing event. The required
       columns are the ones specified in `isoform1_junctions`,
        `isoform2_junctions`, and `event_col`.
    junction_reads_2d : pandas.DataFrame
        A (samples, junctions) dataframe of the junction reads found in each
        sample
    reads_col : str, optional (default "reads")
        The name of the column in `splice_junction_reads` which represents the
        number of reads observed at a splice junction of a particular sample.
    min_reads : int, optional (default 10)
        The minimum number of reads that need to be observed at each junction
        for an event to be counted.
    isoform1_junctions : list
        Columns in `event_annotation` which represent junctions that
        correspond to isoform1, the Psi=0 isoform, e.g. ['junction13'] for SE
        (junctions between exon1 and exon3)
    isoform2_junctions : list
        Columns in `event_annotation` which represent junctions that
        correspond to isoform2, the Psi=1 isoform, e.g.
        ['junction12', 'junction23'] (junctions between exon1, exon2, and
        junction between exon2 and exon3)
    event_col : str
        Column in `event_annotation` which is a unique identifier for each
        row, e.g.

    Returns
    -------
    psi : pandas.DataFrame
        An (samples, events) dataframe of the percent spliced-in values
    """
    log = logging.getLogger('outrigger.psi.calculate_psi')

    if debug:
        log.setLevel(10)

    psis = _maybe_parallelize_psi(event_annotation, junction_reads_2d,
                                  isoform1_junctions, isoform2_junctions,
                                  reads_col, min_reads, method, multiplier,
                                  n_jobs, debug, log)

    # use only non-empty psi outputs
    psis = filter(lambda x: x is not None, psis)
    try:
        psi_df = pd.concat(psis, axis=1)
    except ValueError:
        psi_df = pd.DataFrame(index=junction_reads_2d.index.levels[1])
    done(n_tabs=3)
    psi_df.index.name = junction_reads_2d.index.names[1]
    return psi_df
