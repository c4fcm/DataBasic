import logging
import operator
import os
import re
import sys
from collections import OrderedDict
from databasic import mongo, get_base_dir
from databasic.forms import ConnectTheDotsUpload, ConnectTheDotsSample, ConnectTheDotsPaste
from databasic.logic import connectthedots as ctd, filehandler
from flask import Blueprint, g, redirect, render_template, request, Response, send_from_directory
from natural.number import ordinal


mod = Blueprint('connectthedots', __name__,
                url_prefix='/<lang_code>/connectthedots',
                template_folder='../templates/connectthedots')

logger = logging.getLogger(__name__)


@mod.route('/', methods=('GET', 'POST'))
def index():
    """
    POST request and redirect to results; otherwise, show the homepage
    """
    forms = OrderedDict()
    forms['sample'] = ConnectTheDotsSample(g.current_lang)
    forms['paste'] = ConnectTheDotsPaste()
    forms['upload'] = ConnectTheDotsUpload()
    input_error = None

    if request.method == 'POST':
        btn_value = request.form['btn']
        sample_id = ''
        results = None

        # Sample file
        if btn_value == 'sample':
            sample_source = forms['sample'].data['sample']

            # check to see if file exists in cache already
            existing_doc_id = mongo.results_for_sample('connectthedots', sample_source)
            if existing_doc_id is not None:
                sample_name = filehandler.get_sample_title(sample_source)
                logger.debug('[CTD] Doc exists in database, redirecting to cached version: %s', sample_name)
                return redirect(request.url + 'results/' + existing_doc_id)
            else:
                sample_name = filehandler.get_sample_title(sample_source)
                sample_id = sample_source
                logger.debug('[CTD] New doc from sample: %s', sample_name)
                results = process_sample(sample_source)

        # Paste table
        elif btn_value == 'paste':
            pasted_text = forms['paste'].data['area']
            has_header_row = forms['paste'].data['has_header_row']
            logger.debug('[CTD] New doc from paste')
            results = process_paste(pasted_text, has_header_row)

        # File upload
        elif btn_value == 'upload':
            upload_file = forms['upload'].data['upload']
            has_header_row = forms['upload'].data['has_header_row']
            logger.debug('[CTD] New doc from upload: %s', upload_file.filename)
            results = process_upload(upload_file, has_header_row)

        if (results is not None) and (btn_value is not None) and (btn_value != '') and ('json' in results):
            logger.debug('[CTD] Redirecting to render new doc from %s', btn_value)
            return redirect_to_results(results, btn_value, sample_id)
        else:
            input_error = btn_value

    return render_template('connectthedots.html',
                           forms=list(forms.items()),
                           tool_name='connectthedots',
                           max_file_size_in_mb=g.max_file_size_mb,
                           input_error=input_error)


def process_sample(source):
    """
    Return results for a sample file
    """
    sample_file = filehandler.get_sample(source)
    sample_path = sample_file['path']
    sample_name = sample_file['title']
    logger.debug('[CTD] Loading from: %s', sample_path)

    results = ctd.get_summary(sample_path)
    results['has_multiple_sheets'] = False
    results['filename'] = sample_name
    results['biography'] = sample_file['biography']

    return results


def process_paste(text, has_header_row=True):
    """
    Return results for a pasted table
    """
    rows = text.splitlines()
    csv_rows = []

    for r in rows:
        groups = re.findall(r'"(.*?)+"|\t', r)
        if len(groups) == 3:
            csv_rows.append((groups[0], groups[2]))
        elif len(groups) == 1:
            csv_rows.append((r.split('\t')[0], r.split('\t')[1]))
        else:
            return None

    headers = csv_rows.pop(0) if has_header_row else ['source', 'target']
    file_path = filehandler.write_to_csv(headers, csv_rows)
    file_size = os.stat(file_path).st_size
    logger.debug('[CTD] File size: %d bytes', file_size)

    data = ctd.get_summary(file_path)
    data['has_multiple_sheets'] = False
    data['filename'] = 'Your Pasted Data'

    filehandler.delete_files([file_path])
    return data


def process_upload(file_object, has_header_row=True):
    """
    Return results for an uploaded file
    """
    file_path = filehandler.open_doc(file_object)
    file_name = file_object.filename
    file_size = os.stat(file_path).st_size
    logger.debug('[CTD] File size: %d bytes', file_size)

    csv_paths = filehandler.convert_to_csv(file_path)
    results = ctd.get_summary(csv_paths[0], has_header_row)  # only use first sheet
    results['has_multiple_sheets'] = True if len(csv_paths) > 1 else False
    results['filename'] = file_name

    filehandler.delete_files(csv_paths)
    return results


def redirect_to_results(results, source, sample_id=''):
    """
    Redirect to results page
    """
    logger.debug('[CTD] Saving CSV and redirecting to result')
    doc_id = mongo.save_csv('connectthedots', results, sample_id, source)
    logger.debug('[CTD] Saved CSV and doc_id is %s', doc_id)
    return redirect(request.url + 'results/' + doc_id + '?submit=true')


@mod.route('/results/<doc_id>')
def results_by_id(doc_id):
    """
    Lookup results for a given document
    """
    try:
        logger.info('[CTD] Showing results for doc: %s', doc_id)
        return render_results(doc_id)
    except Exception as e:
        logger.warning('[CTD] Unable to find doc: %s', doc_id)
        logger.warning('[CTD] Error: %s', str(e))
        logger.warning('[CTD] Error: %s', sys.exc_info()[0])
        return render_template('no_results.html', tool_name='connectthedots')


def render_results(doc_id):
    """
    Render results page
    """
    doc = mongo.find_document('connectthedots', doc_id)
    results = doc.get('results')

    if doc.get('source') != 'sample':
        remaining_days = mongo.get_remaining_days('connectthedots', doc_id)
    else:
        remaining_days = None

    first_mismatch = None  # get first centrality/degree mismatch
    degree_index = 0
    centrality_index = 0
    table_by_degree = sorted(results['table'], key=operator.itemgetter('degree'), reverse=True)
    table_by_centrality = results['table']

    for i, row in enumerate(table_by_degree):
        if row['id'] != table_by_centrality[i]['id']:
            first_mismatch = row['id']
            degree_index = i
            break

    if first_mismatch is not None:
        for i, row in enumerate(table_by_centrality[degree_index + 1:]):  # start from where we left off
            if row['id'] == first_mismatch:
                centrality_index = i + degree_index + 1
                break

    what_next = {
        'mismatch_id': first_mismatch,
        'mismatch_degree': ordinal(degree_index + 1),
        'mismatch_centrality': ordinal(centrality_index + 1),
        'lowest_degree': table_by_degree[-1]['id']
    }

    biography = results['biography'] if 'biography' in results else None

    return render_template('connectthedots/results.html', 
                           results=results,
                           whatnext=what_next,
                           tool_name='connectthedots',
                           source=doc['source'],
                           has_multiple_sheets=results['has_multiple_sheets'],
                           remaining_days=remaining_days,
                           biography=biography)


@mod.route('/results/<doc_id>/graph.gexf')
def download_gexf(doc_id):
    """
    Download GEXF file
    """
    logger.info('[CTD] Requesting GEXF for doc: %s', doc_id)
    doc = mongo.find_document('connectthedots', doc_id)
    return Response(doc.get('results')['gexf'], mimetype='application/xml')


@mod.route('/results/<doc_id>/table.csv')
def download_table(doc_id):
    """
    Download CSV of degree/centrality scores
    """
    logger.info('[CTD] Requesting CSV of table for doc: %s', doc_id)
    doc = mongo.find_document('connectthedots', doc_id)

    def as_csv(rows, headers):
        yield ','.join(headers) + '\n'
        for r in rows:
            yield ','.join(map(str, [r['id'], r['degree'], r['centrality'], r['community']])) + '\n'

    return Response(
        as_csv(doc.get('results')['table'], ['node', 'degree', 'betweenness centrality', 'community']),
        mimetype='text/csv')


@mod.route('/ctd-template.csv')
def download_user_template():
    filename = "ctd-template.csv"
    dir_path = os.path.join(get_base_dir(), 'databasic', 'static', 'files', 'user-templates', g.current_lang)
    logger.debug("download user template from %s/%s", dir_path, filename)
    return send_from_directory(directory=dir_path, filename=filename)


@mod.route('/connect-the-dots-activity-guide.pdf')
def download_activity_guide():
    filename = "Connect the Dots Activity Guide.pdf"
    dir_path = os.path.join(get_base_dir(), 'databasic', 'static', 'files', 'activity-guides', g.current_lang)
    logger.debug("download activity guide from %s/%s", dir_path, filename)
    return send_from_directory(directory=dir_path, filename=filename)
