#!/usr/bin/env python3
# Copyright (c) 2014 Wladimir J. van der Laan
# Distributed under the MIT software license, see the accompanying
# file COPYING or http://www.opensource.org/licenses/mit-license.php.
'''
Run this script from the root of the repository to update all translations from
transifex.
It will do the following automatically:
- fetch all translations using the tx tool
(https://github.com/transifex/transifex-client / pip install transifex-client)
- post-process them into valid and committable format
  - remove invalid control characters
  - remove location tags (makes diffs less noisy)
  - attempt to fix some translations

TODO:
- auto-add new translations to the build system according to the translation process
'''
import subprocess
import re
import sys
import os
import io
import xml.etree.ElementTree as ET

# Name of transifex tool
TX = 'tx'
# Name of source language file
SOURCE_LANG = 'bitcoin_en.ts'
# Directory with locale files
LOCALE_DIR = 'src/qt/locale'
# Minimum number of messages for translation to be considered at all
MIN_NUM_MESSAGES = 10
# Regexp to check for Bitcoin addresses
ADDRESS_REGEXP = re.compile('([13]|bc1)[a-zA-Z0-9]{30,}')

def check_at_repository_root():
    if not os.path.exists('.git'):
        print('No .git directory found')
        print('Execute this script at the root of the repository', file=sys.stderr)
        sys.exit(1)

def fetch_all_translations():
    if subprocess.call([TX, 'pull', '-f', '-a']):
        print('Error while fetching translations', file=sys.stderr)
        sys.exit(1)

def find_format_specifiers(s):
    '''Find all format specifiers in a string.'''
    pos = 0
    specifiers = []
    while True:
        percent = s.find('%', pos)
        if percent < 0:
            break
        specifiers.append(s[percent+1])
        pos = percent+2
    return specifiers

def split_format_specifiers(specifiers):
    '''Split format specifiers between numeric (Qt) and others (strprintf)'''
    numeric = []
    other = []
    for s in specifiers:
        if s in {'1','2','3','4','5','6','7','8','9'}:
            numeric.append(s)
        else:
            other.append(s)

    # If both numeric format specifiers and "others" are used, assume we're dealing
    # with a Qt-formatted message. In the case of Qt formatting (see https://doc.qt.io/qt-5/qstring.html#arg)
    # only numeric formats are replaced at all. This means "(percentage: %1%)" is valid, without needing
    # any kind of escaping that would be necessary for strprintf. Without this, this function
    # would wrongly detect '%)' as a printf format specifier.
    if numeric:
        other = []

    # numeric (Qt) can be present in any order, others (strprintf) must be in specified order
    return set(numeric),other

def sanitize_string(s):
    '''Sanitize string for printing'''
    return s.replace('\n',' ')

def check_format_specifiers(source, translation, errors, numerus):
    global source_f
    source_f = split_format_specifiers(find_format_specifiers(source))
    # assert that no source messages contain both Qt and strprintf format specifiers
    # if this fails, go change the source as this is hacky and confusing!
    assert(not(source_f[0] and source_f[1]))
    try:
        translation_f = split_format_specifiers(find_format_specifiers(translation))
    except IndexError:
        errors.append("Parse error in translation for '%s': '%s'" % (sanitize_string(source), sanitize_string(translation)))
        return False
    else:
        if source_f != translation_f:
            if numerus and source_f == (set(), ['n']) and translation_f == (set(), []) and translation.find('%') == -1:
                # Allow numerus translations to omit %n specifier (usually when it only has one possible value)
                return True
            errors.append("Mismatch between '%s' and '%s'" % (sanitize_string(source), sanitize_string(translation)))
            return False
    return True

def all_ts_files(suffix=''):
    for filename in os.listdir(LOCALE_DIR):
        # process only language files, and do not process source language
        if not filename.endswith('.ts'+suffix) or filename == SOURCE_LANG+suffix:
            continue
        if suffix: # remove provided suffix
            filename = filename[0:-len(suffix)]
        filepath = os.path.join(LOCALE_DIR, filename)
        yield(filename, filepath)

def fix_string(s):
    '''Fix most common (format specifiers related) mistakes'''
    return s.replace('% 1','%1').replace('1%','%1').replace('$1','%1').replace('2%','%2').replace('% s','%s').replace('s%','%s').replace('$s','%s').replace('n%','%n').replace('$n','%n').replace('% n','%n').replace('% d','%d')
			
FIX_RE = re.compile(b'[\x00-\x09\x0b\x0c\x0e-\x1f]')
def remove_invalid_characters(s):
    '''Remove invalid characters from translation string'''
    return FIX_RE.sub(b'', s)

# Override cdata escape function to make our output match Qt's (optional, just for cleaner diffs for
# comparison, disable by default)
_orig_escape_cdata = None
def escape_cdata(text):
    text = _orig_escape_cdata(text)
    text = text.replace("'", '&apos;')
    text = text.replace('"', '&quot;')
    return text

def contains_bitcoin_addr(text, errors):
    if text is not None and ADDRESS_REGEXP.search(text) is not None:
        errors.append('Translation "%s" contains a bitcoin address. This will be removed.' % (text))
        return True
    return False

def clear_translation(t):
    t.clear()
    t.set('type', 'unfinished')

def postprocess_translations(reduce_diff_hacks=False):
    global source_f
    global tf #translations fixed
    global lr #languages removed
    tf = lr = 0
	
    print('Checking and postprocessing...')

    if reduce_diff_hacks:
        global _orig_escape_cdata
        _orig_escape_cdata = ET._escape_cdata
        ET._escape_cdata = escape_cdata

    for (filename,filepath) in all_ts_files():
        os.rename(filepath, filepath+'.orig')

    have_errors = False
    for (filename,filepath) in all_ts_files('.orig'):
        # pre-fixups to cope with transifex output
        parser = ET.XMLParser(encoding='utf-8') # need to override encoding because 'utf8' is not understood only 'utf-8'
        with open(filepath + '.orig', 'rb') as f:
            data = f.read()
        # remove control characters; this must be done over the entire file otherwise the XML parser will fail
        data = remove_invalid_characters(data)
        tree = ET.parse(io.BytesIO(data), parser=parser)

        # iterate over all messages in file
        root = tree.getroot()
        for context in root.findall('context'):
            for message in context.findall('message'):
                numerus = message.get('numerus') == 'yes'
                source = message.find('source').text
                translation_node = message.find('translation')
                # pick all numerusforms
                if numerus:
                    translations = [i.text for i in translation_node.findall('numerusform')]
                else:
                    translations = [translation_node.text]

                for translation in translations:
                    if translation is None:
                        continue
                    errors = []
                    valid = check_format_specifiers(source, translation, errors, numerus) and not contains_bitcoin_addr(translation, errors)

                    for error in errors:
                        print('%s: %s' % (filename, error))

                    # check if translation can be fixed
                    if not valid:
                       if translation_node.text != None: # only attempt to fix if translation is not a NoneType object
                           translation_node.text = fix_string(translation_node.text) #fix most common mistakes by replacing symbols
                           translation_f = split_format_specifiers(find_format_specifiers(translation_node.text))
                           if source_f == translation_f: # check if translation is acceptable after fixing it.
                               # if the translation seems okay, add spaces before % if needed - only if certain strings are not found, and if '%' is not the first symbol in a string.
                               if translation_node.text[0] != "%" and translation_node.text.find(' %') == -1 and translation_node.text.find('(%') == -1 and translation_node.text.find('\'%') == -1 and translation_node.text.find('\"%') == -1:
                                   translation_node.text = translation_node.text.replace('%',' %')
                               tf = tf + 1	
                               print('Translation #', tf, 'fixed:', translation_node.text)
                               print('')
                           # check if translation contains '%' and if source contains '&'
                           # check if translation contains '%1' and if source contains '%n'
                           # If so, replace accordingly.
                           elif translation_node.text.find('%') >= 0 and source.find('&') >= 0: #
                               translation_node.text = translation_node.text.replace('%', '&')
                               translation_f = split_format_specifiers(find_format_specifiers(translation_node.text))
                               if source_f == translation_f:
                                   tf = tf + 1
                                   print('Translation #', tf, 'fixed:', translation_node.text)
                                   print('')
                               else:
                                   print('Translation could not be fixed')
                                   print('')
                                   have_errors = True	
                                   clear_translation(translation_node)
                           elif translation_node.text.find('%1') >= 0 and source.find('%n') >= 0:
                               translation_node.text = translation_node.text.replace('%1', '%n')
                               translation_f = split_format_specifiers(find_format_specifiers(translation_node.text))
                               if source_f == translation_f:
                                   tf = tf + 1
                                   print('Translation #', tf, 'fixed:', translation_node.text)
                                   print('')
                               else:
                                   print('Translation could not be fixed')
                                   print('')
                                   have_errors = True
                                   clear_translation(translation_node)
                           else:
                               print('Translation could not be fixed')
                               print('')
                               have_errors = True	
                               clear_translation(translation_node)
                       else:
                           print('TypeNone object, cannot try to fix this string.')
                           print('')
                           have_errors = True	
                           clear_translation(translation_node)

                # Remove entire message if it is an unfinished translation
                if translation_node.get('type') == 'unfinished':
                    context.remove(message)

                # Remove location tags
                for location in message.findall('location'):
                    message.remove(location)

        # check if document is (virtually) empty, and remove it if so
        num_messages = 0
        for context in root.findall('context'):
            for message in context.findall('message'):
                num_messages += 1
        if num_messages < MIN_NUM_MESSAGES:
            print('#', lr, ': Removing %s, as it contains only %i messages' % (filepath, num_messages))
            lr=lr+1
            continue

        # write fixed-up tree
        # if diff reduction requested, replace some XML to 'sanitize' to qt formatting
        if reduce_diff_hacks:
            out = io.BytesIO()
            tree.write(out, encoding='utf-8')
            out = out.getvalue()
            out = out.replace(b' />', b'/>')
            with open(filepath, 'wb') as f:
                f.write(out)
        else:
            tree.write(filepath, encoding='utf-8')
    return have_errors

def delete_files():
    delete_original_files = input('Would you like to delete original files (Y/N)?\n')
    if delete_original_files in ['y', 'Y', 'yes', 'Yes']:
        for item in os.listdir(LOCALE_DIR):
            if item.endswith(".orig"):
                os.remove(os.path.join(LOCALE_DIR, item))
        print("Original files deleted.")
    elif delete_original_files in ['n', 'N']:
        print("Original files not deleted.")
    else:
        print("No acceptable input given.")
        delete_files()

if __name__ == '__main__':
    check_at_repository_root()
    fetch_all_translations()
    postprocess_translations()
    print('')
    print('Total translations fixed:', tf)
    print('Total languages removed:', lr)
    delete_files()
