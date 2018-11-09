# -*- coding: utf-8 -*-

"""Console script for jenkins_triage."""
from collections import defaultdict
import difflib
import pprint
import re
import sys

from bs4 import BeautifulSoup
import click
import jenkins
from jenkins import Jenkins
import requests

server = None

def job_failure_without_build(line):
    print 'Failure without build'
    match = re.search(u'.* » (?P<jobName>.*) completed with result FAILURE', line)
    if match:
        print server.get_job_info(match.group('jobName'))
        return (match.group('jobName'), 'latest')

    return (None, None)
    

def job_success_with_build(line):
#     print 'Success with build'
    match = re.search('Finished Build : #(?P<buildNum>\d*) of Job : (?P<jobName>.*) with status : SUCCESS', line)
    if match:
        return (match.group('jobName'), int(match.group('buildNum')))

    return (None, None)


def job_success_without_build(line):
#     print u'Success without build: {}'.format(line)
    match = re.search(u'.* » (?P<viewName>.*),beaker completed with result SUCCESS', line)
    if match:
        view_name = match.group('viewName').replace('PLATFORM=NONE,', 'PLATFORM=NONE,SCM_BRANCH=')
        return (view_name, 'latest')

    return (None, None)
    

def job_failure_with_build(line):
    print 'Failure with build'
    match = re.search('Finished Build : #(?P<buildNum>\d*) of Job : (?P<jobName>.*) with status : FAILURE', line)
    if match:
        return (match.group('jobName'), int(match.group('buildNum')))

    return (None, None)


def job_failure(output):
    global server
    lines = output.splitlines()

    errors = []
    for line in lines:
        job_matchers = [job_failure_with_build,
                        job_failure_without_build,]
        for matcher in job_matchers:
            jobname, build_num = matcher(line)
            if jobname:
#                print 'Jobname: ' + jobname
                # What to do if job doesn't exist? Check for job?
                output = server.get_build_console_output(jobname, build_num)
                finder = get_strategy(output)
                errors.append(finder(output))
                break

    return [e for j in errors for e in j]


def job_success(output, jobname, build_num, **kwargs):
    global server
    lines = output.splitlines()

    errors = []
    for line in lines:
        job_matchers = [job_success_with_build,
                        job_success_without_build,]
        for matcher in job_matchers:
            viewname, _ = matcher(line)
#             print viewname, build_num
            if viewname:
#                 print 'Viewname: ' + viewname
                # What to do if job doesn't exist? Check for job?
                output = get_view_output(jobname, viewname, build_num)
                finder = get_strategy(output, kwargs)
                errors.append(delimited(output, **kwargs))
                break

    return [e for j in errors for e in j]


def console_failure(output):
    global server
    lines = output.splitlines()
    ignore_text = [
        'ERROR: No submodules found.',
        'Failed to resolve parameters in string SHA=\$\{enterprise_dist_sha\} due to following error:',
        'Loading robots.txt; please ignore errors.',
        '.* ERROR 404: Not Found.',
        ]
    error_lines = []
    for line in lines:
        if 'error' in line.lower():
            ignored = any([re.search(regex, line) for regex in ignore_text])
            if not ignored:
                error_lines.append(line)

    return error_lines


def get_view_output(jobname, view, build_num):
    url = 'https://cinext-jenkinsmaster-enterprise-prod-1.delivery.puppetlabs.net/job/{}/LAYOUT={},label=beaker/{}/consoleText'.format(jobname, view, build_num)
    req = jenkins.requests.Request('GET', url)
    html_doc = server.jenkins_open(req)
    soup = BeautifulSoup(html_doc, 'html.parser')
    output = soup.get_text()

    return output


def delimited(output, start_delimiter, end_delimiter):
    lines = output.splitlines()

    started = None
    ended = None
    check_lines = []
    for line in lines:
#        print line
        if start_delimiter in line:
            started = True
            ended = False
            continue

        if end_delimiter in line:
            started = False
            ended = True
            continue

        if started and not ended:
            check_lines.append(line)

    return check_lines


def get_strategy(output, start_delimiter=None, end_delimiter=None):
#    print(u"Strategy output: {}".format(output))
    if 'status : SUCCESS' in output or 'completed with result SUCCESS' in output:
#        print('Selecting job success')
        return job_success

    if 'status : FAILURE' in output or 'completed with result FAILURE' in output:
#        print('Job failure')
        return job_failure

    if start_delimiter and end_delimiter:
#        print('Selecting delimited')
        return delimited

#    print('Selecting console failure')
    return console_failure


@click.group()
def cli():
    pass


@cli.command()
@click.argument("job_name")
@click.option("--jenkins-username", envvar='JENKINS_USERNAME', required=True, help='Can be provided by setting the JENKINS_USERNAME environment variable.')
@click.option("--jenkins-token", prompt=True, hide_input=True, envvar='JENKINS_TOKEN', required=True, help='Can be provided by setting the JENKINS_TOKEN environment variable. Will prompt if this option is not provided.')
def errors(job_name, jenkins_username, jenkins_token):
    """Console script for jenkins_triage."""
    global server
#    job_name = 'enterprise_pe-acceptance-tests_integration-system_pe_full-upgrade_weekend_2016.4.x' # 'enterprise_pe-orchestrator_intn-van-sys-pez-multi_2016.4.x-2016.4.x' # 'enterprise_pe-modules-vanagon-suite_intn-van-sys-pez-multi_daily-pe-modules-2016.4.x'
    server = Jenkins('https://cinext-jenkinsmaster-enterprise-prod-1.delivery.puppetlabs.net', username=jenkins_username, password=jenkins_token)
    info = server.get_job_info(job_name)
    builds = [server.get_build_info(job_name, build['number']) for build in info['builds']]
    failed_build_numbers = [b for b in builds if b['result']  == 'FAILURE']
    last_job_errors = None

    counts = defaultdict(int)
    similar = set()
    for build in failed_build_numbers:
        output = server.get_build_console_output(job_name, build['number'])
        finder = get_strategy(output)
        errors = finder(output)
        print "Errors: {}".format(errors)
        if last_job_errors:
            seq = difflib.SequenceMatcher(a=last_job_errors, b=errors)
            if seq.ratio() == 1.0:
                counts['exact'] += 1
            if seq.ratio() >= 0.7 and seq.ratio() < 1.0:
                counts['similar'] += 1
                similar.append(errors)
        else:
            last_job_errors = errors

    if last_job_errors:
        click.echo('Last job errors were:')
        click.echo('\t{}'.format('\n\t'.join(last_job_errors)))

    if last_job_errors and 'exact' in counts:
        click.echo('There were {} jobs that failed with errors exactly the same as the last failed job:'.format(counts['exact']))
        click.echo('\t{}'.format('\n\t'.join(last_job_errors)))

    if last_job_errors and 'similar' in counts:
        click.echo('There were {} jobs that failed with experienced similar errors as the last failed job:'.format(counts['exact']))
        click.echo('\t{}'.format('\n\t'.join(last_job_errors)))
        for s in similar:
            click.echo('Additional Failed Job:')
            click.echo('\t{}'.format('\n\t'.join(s)))


@cli.command()
@click.argument("job_name")
@click.option("--start-delimiter", required=True)
@click.option("--end-delimiter", required=True)
@click.option("--jenkins-username", envvar='JENKINS_USERNAME', required=True, help='Can be provided by setting the JENKINS_USERNAME environment variable.')
@click.option("--jenkins-token", prompt=True, hide_input=True, envvar='JENKINS_TOKEN', required=True, help='Can be provided by setting the JENKINS_TOKEN environment variable. Will prompt if this option is not provided.')
def gather(job_name, start_delimiter, end_delimiter, jenkins_username, jenkins_token):
    global server
#    job_name = 'enterprise_pe-acceptance-tests_integration-system_pe_full-upgrade_weekend_2016.4.x' # 'enterprise_pe-orchestrator_intn-van-sys-pez-multi_2016.4.x-2016.4.x' # 'enterprise_pe-modules-vanagon-suite_intn-van-sys-pez-multi_daily-pe-modules-2016.4.x'
    server = Jenkins('https://cinext-jenkinsmaster-enterprise-prod-1.delivery.puppetlabs.net', username=jenkins_username, password=jenkins_token)
    info = server.get_job_info(job_name)
    builds = [server.get_build_info(job_name, build['number']) for build in info['builds']]
    last_job_check_lines = None
    counts = defaultdict(int)
    similar = set()
    for build in builds:
        output = server.get_build_console_output(job_name, build['number'])
#        print output
        try:
            req = jenkins.requests.Request('GET', 'https://cinext-jenkinsmaster-enterprise-prod-1.delivery.puppetlabs.net/view/__experimental/job/experimental_pe-acceptance-tests_integration-system_pe_full_nightly_2018.1.x/{}/LAYOUT=ubuntu1604-64mcd-64f-32f,LEGACY_AGENT_VERSION=NONE,PLATFORM=NONE,SCM_BRANCH=2018.1.x,UPGRADE_FROM=NONE,UPGRADE_TO_VERSION=NONE,label=beaker/testReport/(root)/acceptance_tests_01_post_install_tests/idempotent_rb'.format(build['number']))
            html_doc = server.jenkins_open(req)
        except:
            continue

        soup = BeautifulSoup(html_doc, 'html.parser')
#        output = soup.pre.get_text()

        finder = get_strategy(output, start_delimiter=start_delimiter, end_delimiter=end_delimiter)
        found_lines = finder(output, job_name, build['number'], start_delimiter=start_delimiter, end_delimiter=end_delimiter)

        print("Found lines: ".format(found_lines))
        if last_job_check_lines:
            seq = difflib.SequenceMatcher(a=last_job_check_lines, b=found_lines)

            if seq.ratio() == 1.0:
                counts['exact'] += 1
            if seq.ratio() >= 0.7 and seq.ratio() < 1.0:
                counts['similar'] += 1
                similar.append(found_lines)
        else:
            last_job_check_lines = found_lines

    if last_job_check_lines:
        click.echo('Last job check_lines were:')
        click.echo('\t{}'.format('\n\t'.join(last_job_check_lines)))

    if last_job_check_lines and 'exact' in counts:
        click.echo('There were {} jobs that failed with check_lines exactly the same as the last failed job:'.format(counts['exact']))
        click.echo('\t{}'.format('\n\t'.join(last_job_check_lines)))

    if last_job_check_lines and 'similar' in counts:
        click.echo('There were {} jobs that failed with experienced similar check_lines as the last failed job:'.format(counts['exact']))
        click.echo('\t{}'.format('\n\t'.join(last_job_check_lines)))
        for s in similar:
            click.echo('Additional Failed Job:')
            click.echo('\t{}'.format('\n\t'.join(s)))
    
if __name__ == "__main__":
    sys.exit(errors())  # pragma: no cover
