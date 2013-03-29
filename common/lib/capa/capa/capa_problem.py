#
# File:   capa/capa_problem.py
#
# Nomenclature:
#
# A capa Problem is a collection of text and capa Response questions.
# Each Response may have one or more Input entry fields.
# The capa problem may include a solution.
#
'''
Main module which shows problems (of "capa" type).

This is used by capa_module.
'''

from __future__ import division

from datetime import datetime
import json
import logging
import math
import numpy
import os
import random
import re
import scipy
import struct
import sys

from lxml import etree
from xml.sax.saxutils import unescape
from copy import deepcopy

import chem
import chem.chemcalc
import chem.chemtools
import chem.miller
import verifiers
import verifiers.draganddrop

import calc
from .correctmap import CorrectMap
import eia
import inputtypes
import customrender
from .util import contextualize_text, convert_files_to_filenames
import xqueue_interface

# to be replaced with auto-registering
import responsetypes

# dict of tagname, Response Class -- this should come from auto-registering
response_tag_dict = dict([(x.response_tag, x) for x in responsetypes.__all__])

# extra things displayed after "show answers" is pressed
solution_tags = ['solution']

# these get captured as student responses
response_properties = ["codeparam", "responseparam", "answer", "openendedparam"]

# special problem tags which should be turned into innocuous HTML
html_transforms = {'problem': {'tag': 'div'},
                   "text": {'tag': 'span'},
                   "math": {'tag': 'span'},
                   }

global_context = {'random': random,
                  'numpy': numpy,
                  'math': math,
                  'scipy': scipy,
                  'calc': calc,
                  'eia': eia,
                  'chemcalc': chem.chemcalc,
                  'chemtools': chem.chemtools,
                  'miller': chem.miller,
                  'draganddrop': verifiers.draganddrop}

# These should be removed from HTML output, including all subelements
html_problem_semantics = ["codeparam", "responseparam", "answer", "script", "hintgroup", "openendedparam", "openendedrubric"]

log = logging.getLogger(__name__)

#-----------------------------------------------------------------------------
# main class for this module


class LoncapaProblem(object):
    '''
    Main class for capa Problems.
    '''

    def __init__(self, problem_text, id, state=None, seed=None, system=None):
        '''
        Initializes capa Problem.

        Arguments:

         - problem_text (string): xml defining the problem
         - id           (string): identifier for this problem; often a filename (no spaces)
         - state        (dict): student state
         - seed         (int): random number generator seed (int)
         - system       (ModuleSystem): ModuleSystem instance which provides OS,
                                        rendering, and user context

        '''

        ## Initialize class variables from state
        self.do_reset()
        self.problem_id = id
        self.system = system
        if self.system is None:
            raise Exception()

        state = state if state else {}

        # Set seed according to the following priority:
        #       1. Contained in problem's state
        #       2. Passed into capa_problem via constructor
        #       3. Assign from the OS's random number generator
        self.seed = state.get('seed', seed)
        if self.seed is None:
            self.seed = struct.unpack('i', os.urandom(4))[0]
        self.student_answers = state.get('student_answers', {})
        if 'correct_map' in state:
            self.correct_map.set_dict(state['correct_map'])
        self.done = state.get('done', False)
        self.input_state = state.get('input_state', {})



        # Convert startouttext and endouttext to proper <text></text>
        problem_text = re.sub("startouttext\s*/", "text", problem_text)
        problem_text = re.sub("endouttext\s*/", "/text", problem_text)
        self.problem_text = problem_text

        # parse problem XML file into an element tree
        self.tree = etree.XML(problem_text)

        # handle any <include file="foo"> tags
        self._process_includes()

        # construct script processor context (eg for customresponse problems)
        self.context = self._extract_context(self.tree, seed=self.seed)

        # Pre-parse the XML tree: modifies it to add ID's and perform some in-place
        # transformations.  This also creates the dict (self.responders) of Response
        # instances for each question in the problem. The dict has keys = xml subtree of
        # Response, values = Response instance
        self._preprocess_problem(self.tree)

        if not self.student_answers:  # True when student_answers is an empty dict
            self.set_initial_display()

        # dictionary of InputType objects associated with this problem
        #   input_id string -> InputType object
        self.inputs = {}

        self.extracted_tree = self._extract_html(self.tree)


    def do_reset(self):
        '''
        Reset internal state to unfinished, with no answers
        '''
        self.student_answers = dict()
        self.correct_map = CorrectMap()
        self.done = False

    def set_initial_display(self):
        """
        Set the student's answers to the responders' initial displays, if specified.
        """
        initial_answers = dict()
        for responder in self.responders.values():
            if hasattr(responder, 'get_initial_display'):
                initial_answers.update(responder.get_initial_display())

        self.student_answers = initial_answers

    def __unicode__(self):
        return u"LoncapaProblem ({0})".format(self.problem_id)

    def get_state(self):
        '''
        Stored per-user session data neeeded to:
            1) Recreate the problem
            2) Populate any student answers.
        '''

        return {'seed': self.seed,
                'student_answers': self.student_answers,
                'correct_map': self.correct_map.get_dict(),
                'done': self.done}

    def get_max_score(self):
        '''
        Return the maximum score for this problem.
        '''
        maxscore = 0
        for response, responder in self.responders.iteritems():
            maxscore += responder.get_max_score()
        return maxscore

    def get_score(self):
        """
        Compute score for this problem.  The score is the number of points awarded.
        Returns a dictionary {'score': integer, from 0 to get_max_score(),
                              'total': get_max_score()}.
        """
        correct = 0
        for key in self.correct_map:
            try:
                correct += self.correct_map.get_npoints(key)
            except Exception:
                log.error('key=%s, correct_map = %s' % (key, self.correct_map))
                raise

        if (not self.student_answers) or len(self.student_answers) == 0:
            return {'score': 0,
                    'total': self.get_max_score()}
        else:
            return {'score': correct,
                    'total': self.get_max_score()}

    def update_score(self, score_msg, queuekey):
        '''
        Deliver grading response (e.g. from async code checking) to
            the specific ResponseType that requested grading

        Returns an updated CorrectMap
        '''
        cmap = CorrectMap()
        cmap.update(self.correct_map)
        for responder in self.responders.values():
            if hasattr(responder, 'update_score'):
                # Each LoncapaResponse will update its specific entries in cmap
                #   cmap is passed by reference
                responder.update_score(score_msg, cmap, queuekey)
        self.correct_map.set_dict(cmap.get_dict())
        return cmap

    def is_queued(self):
        '''
        Returns True if any part of the problem has been submitted to an external queue
        (e.g. for grading.)
        '''
        return any(self.correct_map.is_queued(answer_id) for answer_id in self.correct_map)


    def get_recentmost_queuetime(self):
        '''
        Returns a DateTime object that represents the timestamp of the most recent
        queueing request, or None if not queued
        '''
        if not self.is_queued():
            return None

        # Get a list of timestamps of all queueing requests, then convert it to a DateTime object
        queuetime_strs = [self.correct_map.get_queuetime_str(answer_id)
                          for answer_id in self.correct_map
                          if self.correct_map.is_queued(answer_id)]
        queuetimes = [datetime.strptime(qt_str, xqueue_interface.dateformat)
                      for qt_str in queuetime_strs]

        return max(queuetimes)


    def grade_answers(self, answers):
        '''
        Grade student responses.  Called by capa_module.check_problem.
        answers is a dict of all the entries from request.POST, but with the first part
        of each key removed (the string before the first "_").

        Thus, for example, input_ID123 -> ID123, and input_fromjs_ID123 -> fromjs_ID123

        Calls the Response for each question in this problem, to do the actual grading.
        '''

        # if answers include File objects, convert them to filenames.
        self.student_answers = convert_files_to_filenames(answers)

        # old CorrectMap
        oldcmap = self.correct_map

        # start new with empty CorrectMap
        newcmap = CorrectMap()
        # log.debug('Responders: %s' % self.responders)
        # Call each responsetype instance to do actual grading
        for responder in self.responders.values():
            # File objects are passed only if responsetype explicitly allows for file
            # submissions
            if 'filesubmission' in responder.allowed_inputfields:
                results = responder.evaluate_answers(answers, oldcmap)
            else:
                results = responder.evaluate_answers(convert_files_to_filenames(answers), oldcmap)
            newcmap.update(results)
        self.correct_map = newcmap
        # log.debug('%s: in grade_answers, answers=%s, cmap=%s' % (self,answers,newcmap))
        return newcmap

    def get_question_answers(self):
        """
        Returns a dict of answer_ids to answer values. If we cannot generate
        an answer (this sometimes happens in customresponses), that answer_id is
        not included. Called by "show answers" button JSON request
        (see capa_module)
        """
        # dict of (id, correct_answer)
        answer_map = dict()
        for response in self.responders.keys():
            results = self.responder_answers[response]
            answer_map.update(results)

        # include solutions from <solution>...</solution> stanzas
        for entry in self.tree.xpath("//" + "|//".join(solution_tags)):
            answer = etree.tostring(entry)
            if answer:
                answer_map[entry.get('id')] = contextualize_text(answer, self.context)

        log.debug('answer_map = %s' % answer_map)
        return answer_map

    def get_answer_ids(self):
        """
        Return the IDs of all the responses -- these are the keys used for
        the dicts returned by grade_answers and get_question_answers. (Though
        get_question_answers may only return a subset of these.
        """
        answer_ids = []
        for response in self.responders.keys():
            results = self.responder_answers[response]
            answer_ids.append(results.keys())
        return answer_ids

    def get_html(self):
        '''
        Main method called externally to get the HTML to be rendered for this capa Problem.
        '''
        html = contextualize_text(etree.tostring(self._extract_html(self.tree)), self.context)
        return html


    def handle_input_ajax(self, get):
        '''
        InputTypes can support specialized AJAX calls. Find the correct input and pass along the correct data

        Also, parse out the dispatch from the get so that it can be passed onto the input type nicely
        '''

        # pull out the id
        input_id = get['input_id']
        if self.inputs[input_id]:
            dispatch = get['dispatch']
            return self.inputs[input_id].handle_ajax(dispatch, get)
        else:
            log.warning("Could not find matching input for id: %s" % problem_id)
            return {}



    # ======= Private Methods Below ========

    def _process_includes(self):
        '''
        Handle any <include file="foo"> tags by reading in the specified file and inserting it
        into our XML tree.  Fail gracefully if debugging.
        '''
        includes = self.tree.findall('.//include')
        for inc in includes:
            file = inc.get('file')
            if file is not None:
                try:
                    # open using ModuleSystem OSFS filestore
                    ifp = self.system.filestore.open(file)
                except Exception as err:
                    log.warning('Error %s in problem xml include: %s' % (
                            err, etree.tostring(inc, pretty_print=True)))
                    log.warning('Cannot find file %s in %s' % (
                            file, self.system.filestore))
                    # if debugging, don't fail - just log error
                    # TODO (vshnayder): need real error handling, display to users
                    if not self.system.get('DEBUG'):
                        raise
                    else:
                        continue
                try:
                    # read in and convert to XML
                    incxml = etree.XML(ifp.read())
                except Exception as err:
                    log.warning('Error %s in problem xml include: %s' % (
                            err, etree.tostring(inc, pretty_print=True)))
                    log.warning('Cannot parse XML in %s' % (file))
                    # if debugging, don't fail - just log error
                    # TODO (vshnayder): same as above
                    if not self.system.get('DEBUG'):
                        raise
                    else:
                        continue

                # insert new XML into tree in place of inlcude
                parent = inc.getparent()
                parent.insert(parent.index(inc), incxml)
                parent.remove(inc)
                log.debug('Included %s into %s' % (file, self.problem_id))

    def _extract_system_path(self, script):
        """
        Extracts and normalizes additional paths for code execution.
        For now, there's a default path of data/course/code; this may be removed
        at some point.

        script : ?? (TODO)
        """

        DEFAULT_PATH = ['code']

        # Separate paths by :, like the system path.
        raw_path = script.get('system_path', '').split(":") + DEFAULT_PATH

        # find additional comma-separated modules search path
        path = []

        for dir in raw_path:

            if not dir:
                continue

            # path is an absolute path or a path relative to the data dir
            dir = os.path.join(self.system.filestore.root_path, dir)
            abs_dir = os.path.normpath(dir)
            path.append(abs_dir)

        return path

    def _extract_context(self, tree, seed=struct.unpack('i', os.urandom(4))[0]):  # private
        '''
        Extract content of <script>...</script> from the problem.xml file, and exec it in the
        context of this problem.  Provides ability to randomize problems, and also set
        variables for problem answer checking.

        Problem XML goes to Python execution context. Runs everything in script tags.
        '''
        random.seed(self.seed)
        # save global context in here also
        context = {'global_context': global_context}

        # initialize context to have stuff in global_context
        context.update(global_context)

        # put globals there also
        context['__builtins__'] = globals()['__builtins__']

        # pass instance of LoncapaProblem in
        context['the_lcp'] = self
        context['script_code'] = ''

        self._execute_scripts(tree.findall('.//script'), context)

        return context

    def _execute_scripts(self, scripts, context):
        '''
        Executes scripts in the given context.
        '''
        original_path = sys.path

        for script in scripts:
            sys.path = original_path + self._extract_system_path(script)

            stype = script.get('type')

            if stype:
                if 'javascript' in stype:
                    continue    # skip javascript
                if 'perl' in stype:
                    continue        # skip perl
            # TODO: evaluate only python
            code = script.text
            XMLESC = {"&apos;": "'", "&quot;": '"'}
            code = unescape(code, XMLESC)
            # store code source in context
            context['script_code'] += code
            try:
                # use "context" for global context; thus defs in code are global within code
                exec code in context, context
            except Exception as err:
                log.exception("Error while execing script code: " + code)
                msg = "Error while executing script code: %s" % str(err).replace('<', '&lt;')
                raise responsetypes.LoncapaProblemError(msg)
            finally:
                sys.path = original_path



    def _extract_html(self, problemtree):  # private
        '''
        Main (private) function which converts Problem XML tree to HTML.
        Calls itself recursively.

        Returns Element tree of XHTML representation of problemtree.
        Calls render_html of Response instances to render responses into XHTML.

        Used by get_html.
        '''
        if (problemtree.tag == 'script' and problemtree.get('type')
            and 'javascript' in problemtree.get('type')):
            # leave javascript intact.
            return deepcopy(problemtree)

        if problemtree.tag in html_problem_semantics:
            return

        problemid = problemtree.get('id')    # my ID

        if problemtree.tag in inputtypes.registry.registered_tags():
            # If this is an inputtype subtree, let it render itself.
            status = "unsubmitted"
            msg = ''
            hint = ''
            hintmode = None
            input_id = problemtree.get('id')
            if problemid in self.correct_map:
                pid = input_id
                status = self.correct_map.get_correctness(pid)
                msg = self.correct_map.get_msg(pid)
                hint = self.correct_map.get_hint(pid)
                hintmode = self.correct_map.get_hintmode(pid)

            value = ""
            if self.student_answers and problemid in self.student_answers:
                value = self.student_answers[problemid]

            # do the rendering
            state = {'value': value,
                   'status': status,
                   'id': input_id,
                   'feedback': {'message': msg,
                                'hint': hint,
                                'hintmode': hintmode, }}

            input_type_cls = inputtypes.registry.get_class_for_tag(problemtree.tag)
            # save the input type so that we can make ajax calls on it if we need to
            self.inputs[input_id] = input_type_cls(self.system, problemtree, state)
            return self.inputs[input_id].get_html()

        # let each Response render itself
        if problemtree in self.responders:
            overall_msg = self.correct_map.get_overall_message()
            return self.responders[problemtree].render_html(self._extract_html,
                                                response_msg=overall_msg)

        # let each custom renderer render itself:
        if problemtree.tag in customrender.registry.registered_tags():
            renderer_class = customrender.registry.get_class_for_tag(problemtree.tag)
            renderer = renderer_class(self.system, problemtree)
            return renderer.get_html()

        # otherwise, render children recursively, and copy over attributes
        tree = etree.Element(problemtree.tag)
        for item in problemtree:
            item_xhtml = self._extract_html(item)
            if item_xhtml is not None:
                    tree.append(item_xhtml)

        if tree.tag in html_transforms:
            tree.tag = html_transforms[problemtree.tag]['tag']
        else:
            # copy attributes over if not innocufying
            for (key, value) in problemtree.items():
                tree.set(key, value)

        tree.text = problemtree.text
        tree.tail = problemtree.tail

        return tree

    def _preprocess_problem(self, tree):  # private
        '''
        Assign IDs to all the responses
        Assign sub-IDs to all entries (textline, schematic, etc.)
        Annoted correctness and value
        In-place transformation

        Also create capa Response instances for each responsetype and save as self.responders

        Obtain all responder answers and save as self.responder_answers dict (key = response)
        '''
        response_id = 1
        self.responders = {}
        for response in tree.xpath('//' + "|//".join(response_tag_dict)):
            response_id_str = self.problem_id + "_" + str(response_id)
            # create and save ID for this response
            response.set('id', response_id_str)
            response_id += 1

            answer_id = 1
            input_tags = inputtypes.registry.registered_tags()
            inputfields = tree.xpath("|".join(['//' + response.tag + '[@id=$id]//' + x
                                               for x in (input_tags + solution_tags)]),
                                    id=response_id_str)

            # assign one answer_id for each input type or solution type
            for entry in inputfields:
                entry.attrib['response_id'] = str(response_id)
                entry.attrib['answer_id'] = str(answer_id)
                entry.attrib['id'] = "%s_%i_%i" % (self.problem_id, response_id, answer_id)
                answer_id = answer_id + 1

            # instantiate capa Response
            responder = response_tag_dict[response.tag](response, inputfields,
                                                        self.context, self.system)
            # save in list in self
            self.responders[response] = responder

        # get responder answers (do this only once, since there may be a performance cost,
        # eg with externalresponse)
        self.responder_answers = {}
        for response in self.responders.keys():
            try:
                self.responder_answers[response] = self.responders[response].get_answers()
            except:
                log.debug('responder %s failed to properly return get_answers()',
                          self.responders[response])  # FIXME
                raise

        # <solution>...</solution> may not be associated with any specific response; give
        # IDs for those separately
        # TODO: We should make the namespaces consistent and unique (e.g. %s_problem_%i).
        solution_id = 1
        for solution in tree.findall('.//solution'):
            solution.attrib['id'] = "%s_solution_%i" % (self.problem_id, solution_id)
            solution_id += 1
