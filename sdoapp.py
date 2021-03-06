#!/usr/bin/env python
# -*- coding: UTF-8 -*-

import os
import re
import webapp2
import jinja2
import logging

from markupsafe import Markup, escape # https://pypi.python.org/pypi/MarkupSafe

import parsers


from google.appengine.ext import ndb
from google.appengine.ext import blobstore
from google.appengine.api import users
from google.appengine.ext.webapp import blobstore_handlers

from api import inLayer, read_file, full_path, read_schemas, namespaces, DataCache
from api import Unit, GetTargets, GetSources
from api import GetComment, all_terms, GetAllTypes, GetAllProperties
from api import GetParentList, GetImmediateSubtypes, HasMultipleBaseTypes

logging.basicConfig(level=logging.INFO) # dev_appserver.py --log_level debug .
log = logging.getLogger(__name__)

SCHEMA_VERSION=2.0
sitename = "schema.org"
sitemode = "mainsite" # whitespaced list for CSS tags,
            # e.g. "mainsite testsite" when off expected domains
            # "extensionsite" when in an extension (e.g. blue?)

releaselog = { "2.0": "2015-05-13" }
#
host_ext = ""
myhost = ""
mybasehost = ""

silent_skip_list =  [ "favicon.ico" ] # Do nothing for now

all_layers = {}
ext_re = re.compile(r'([^\w,])+')
PageCache = {}

#TODO: Modes:
# mainsite
# webschemadev
# known extension (not skiplist'd, eg. demo1 on schema.org)

JINJA_ENVIRONMENT = jinja2.Environment(
    loader=jinja2.FileSystemLoader(os.path.join(os.path.dirname(__file__), 'templates')),
    extensions=['jinja2.ext.autoescape'], autoescape=True)

ENABLE_JSONLD_CONTEXT = True
ENABLE_CORS = True
ENABLE_HOSTED_EXTENSIONS = True

ENABLED_EXTENSIONS = [ 'admin', 'auto', 'bib' ]


debugging = False
# debugging = True

def cleanPath(node):
    """Return the substring of a string matching chars approved for use in our URL paths."""
    return re.sub(r'[^a-zA-Z0-9\-/,\.]', '', str(node), flags=re.DOTALL)



class HTMLOutput:
    """Used in place of http response when we're collecting HTML to pass to template engine."""

    def __init__(self):
        self.outputStrings = []

    def write(self, str):
        self.outputStrings.append(str)

    def toHTML(self):
        return Markup ( "".join(self.outputStrings)  )

    def __str__(self):
        return self.toHTML()

# Core API: we have a single schema graph built from triples and units.
# now in api.py


class TypeHierarchyTree:

    def __init__(self):
        self.txt = ""
        self.visited = {}

    def emit(self, s):
        self.txt += s + "\n"

    def toHTML(self):
        return '<ul>%s</ul>' % self.txt

    def toJSON(self):
        return self.txt

    def traverseForHTML(self, node, depth = 1, hashorslash="/", layers='core'):

        """Generate a hierarchical tree view of the types. hashorslash is used for relative link prefixing."""

        # log.debug("traverseForHTML: node=%s hashorslash=%s" % ( node.id, hashorslash ))

        # we are a supertype of some kind
        if len(node.GetImmediateSubtypes(layers=layers)) > 0:

            # and we haven't been here before
            if node.id not in self.visited:
                self.visited[node.id] = True # remember our visit
                self.emit( ' %s<li class="tbranch" id="%s"><a href="%s%s">%s</a>' % (" " * 4 * depth, node.id, hashorslash, node.id, node.id) )
                self.emit(' %s<ul>' % (" " * 4 * depth))

                # handle our subtypes
                for item in node.GetImmediateSubtypes(layers=layers):
                    self.traverseForHTML(item, depth + 1, hashorslash=hashorslash, layers=layers)
                self.emit( ' %s</ul>' % (" " * 4 * depth))
            else:
                # we are a supertype but we visited this type before, e.g. saw Restaurant via Place then via Organization
                seen = '  <a href="#%s">*</a> ' % node.id
                self.emit( ' %s<li class="tbranch" id="%s"><a href="%s%s">%s</a>%s' % (" " * 4 * depth, node.id, hashorslash, node.id, node.id, seen) )

        # leaf nodes
        if len(node.GetImmediateSubtypes(layers=layers)) == 0:
            if node.id not in self.visited:
                self.emit( '%s<li class="tleaf" id="%s"><a href="%s%s">%s</a>%s' % (" " * depth, node.id, hashorslash, node.id, node.id, "" ))
            #else:
                #self.visited[node.id] = True # never...
                # we tolerate "VideoGame" appearing under both Game and SoftwareApplication
                # and would only suppress it if it had its own subtypes. Seems legit.

        self.emit( ' %s</li>' % (" " * 4 * depth) )

    # based on http://danbri.org/2013/SchemaD3/examples/4063550/hackathon-schema.js  - thanks @gregg, @sandro
    def traverseForJSONLD(self, node, depth = 0, last_at_this_level = True, supertype="None", layers='core'):
        emit_debug = False
        if node.id in self.visited:
            # self.emit("skipping %s - already visited" % node.id)
            return
        self.visited[node.id] = True
        p1 = " " * 4 * depth
        if emit_debug:
            self.emit("%s# @id: %s last_at_this_level: %s" % (p1, node.id, last_at_this_level))
        global namespaces;
        ctx = "{}".format(""""@context": {
    "rdfs": "http://www.w3.org/2000/01/rdf-schema#",
    "schema": "http://schema.org/",
    "rdfs:subClassOf": { "@type": "@id" },
    "name": "rdfs:label",
    "description": "rdfs:comment",
    "children": { "@reverse": "rdfs:subClassOf" }
  },\n""" if last_at_this_level and depth==0 else '' )

        unseen_subtypes = []
        for st in node.GetImmediateSubtypes(layers=layers):
            if not st.id in self.visited:
                unseen_subtypes.append(st)
        unvisited_subtype_count = len(unseen_subtypes)
        subtype_count = len( node.GetImmediateSubtypes(layers=layers) )

        supertx = "{}".format( '"rdfs:subClassOf": "schema:%s", ' % supertype.id if supertype != "None" else '' )
        maybe_comma = "{}".format("," if unvisited_subtype_count > 0 else "")
        comment = GetComment(node, layers).strip()
        comment = comment.replace('"',"'")
        comment = re.sub('<[^<]+?>', '', comment)[:60]

        self.emit('\n%s{\n%s\n%s"@type": "rdfs:Class", %s "description": "%s...",\n%s"name": "%s",\n%s"@id": "schema:%s"%s'
                  % (p1, ctx, p1,                 supertx,            comment,     p1,   node.id, p1,        node.id,  maybe_comma))

        i = 1
        if unvisited_subtype_count > 0:
            self.emit('%s"children": ' % p1 )
            self.emit("  %s["  % p1 )
            inner_lastness = False
            for t in unseen_subtypes:
                if emit_debug:
                    self.emit("%s  # In %s > %s i: %s unvisited_subtype_count: %s" %(p1, node.id, t.id, i, unvisited_subtype_count))
                if i == unvisited_subtype_count:
                    inner_lastness = True
                i = i + 1
                self.traverseForJSONLD(t, depth + 1, inner_lastness, supertype=node, layers=layers)

            self.emit("%s  ]%s" % (p1,  "{}".format( "" if not last_at_this_level else '' ) ) )

        maybe_comma = "{}".format( ',' if not last_at_this_level else '' )
        self.emit('\n%s}%s\n' % (p1, maybe_comma))


class Example ():

    @staticmethod
    def AddExample(terms, original_html, microdata, rdfa, jsonld, egmeta, layer='core'):
       """
       Add an Example (via constructor registering it with the terms that it
       mentions, i.e. stored in term.examples).
       """
       # todo: fix partial examples: if (len(terms) > 0 and len(original_html) > 0 and (len(microdata) > 0 or len(rdfa) > 0 or len(jsonld) > 0)):
       if (len(terms) > 0 and len(original_html) > 0 and len(microdata) > 0 and len(rdfa) > 0 and len(jsonld) > 0):
            return Example(terms, original_html, microdata, rdfa, jsonld, egmeta, layer='core')

    def get(self, name, layers='core') :
        """Exposes original_content, microdata, rdfa and jsonld versions (in the layer(s) specified)."""
        if name == 'original_html':
           return self.original_html
        if name == 'microdata':
           return self.microdata
        if name == 'rdfa':
           return self.rdfa
        if name == 'jsonld':
           return self.jsonld

    def __init__ (self, terms, original_html, microdata, rdfa, jsonld, egmeta, layer='core'):
        """Example constructor, registers itself with the relevant Unit(s)."""
        self.terms = terms
        self.original_html = original_html
        self.microdata = microdata
        self.rdfa = rdfa
        self.jsonld = jsonld
        self.egmeta = egmeta
        self.layer = layer
        for term in terms:
            if "id" in egmeta:
              logging.debug("Created Example with ID %s and type %s" % ( egmeta["id"], term.id ))
            term.examples.append(self)



def GetExamples(node, layers='core'):
    """Returns the examples (if any) for some Unit node."""
    return node.examples

def GetExtMappingsRDFa(node, layers='core'):
    """Self-contained chunk of RDFa HTML markup with mappings for this term."""
    if (node.isClass()):
        equivs = GetTargets(Unit.GetUnit("owl:equivalentClass"), node, layers=layers)
        if len(equivs) > 0:
            markup = ''
            for c in equivs:

                if (c.id.startswith('http')):
                  markup = markup + "<link property=\"owl:equivalentClass\" href=\"%s\"/>\n" % c.id
                else:
                  markup = markup + "<link property=\"owl:equivalentClass\" resource=\"%s\"/>\n" % c.id

            return markup
    if (node.isAttribute()):
        equivs = GetTargets(Unit.GetUnit("owl:equivalentProperty"), node, layers)
        if len(equivs) > 0:
            markup = ''
            for c in equivs:
                markup = markup + "<link property=\"owl:equivalentProperty\" href=\"%s\"/>\n" % c.id
            return markup
    return "<!-- no external mappings noted for this term. -->"

def GetJsonLdContext(layers='core'):
    """Generates a basic JSON-LD context file for schema.org."""

    if DataCache.get('JSONLDCONTEXT'):
        log.debug("DataCache: recycled JSONLDCONTEXT")
        return DataCache.get('JSONLDCONTEXT')
    else:
        global namespaces
        jsonldcontext = "{\"@context\":    {\n"
        jsonldcontext += namespaces ;
        jsonldcontext += "        \"@vocab\": \"http://schema.org/\",\n"

        url = Unit.GetUnit("URL")
        date = Unit.GetUnit("Date")
        datetime = Unit.GetUnit("DateTime")

        properties = sorted(GetSources(Unit.GetUnit("typeOf"), Unit.GetUnit("rdf:Property"), layers=layers), key=lambda u: u.id)
        for p in properties:
            range = GetTargets(Unit.GetUnit("rangeIncludes"), p, layers=layers)
            type = None

            if url in range:
                type = "@id"
            elif date in range:
                type = "Date"
            elif datetime in range:
                type = "DateTime"

            if type:
                jsonldcontext += "        \"" + p.id + "\": { \"@type\": \"" + type + "\" },"

        jsonldcontext += "}}\n"
        jsonldcontext = jsonldcontext.replace("},}}","}\n    }\n}")
        jsonldcontext = jsonldcontext.replace("},","},\n")
        DataCache['JSONLDCONTEXT'] = jsonldcontext
        log.debug("DataCache: added JSONLDCONTEXT")
        return jsonldcontext

class ShowUnit (webapp2.RequestHandler):
    """ShowUnit exposes schema.org terms via Web RequestHandler
    (HTML/HTTP etc.).
    """

#    def __init__(self):
#        self.outputStrings = []

    def emitCacheHeaders(self):
        """Send cache-related headers via HTTP."""
        self.response.headers['Cache-Control'] = "public, max-age=43200" # 12h
        self.response.headers['Vary'] = "Accept, Accept-Encoding"

    def GetCachedText(self, node, layers='core'):
        """Return page text from node.id cache (if found, otherwise None)."""
        global PageCache
        cachekey = "%s:%s" % ( layers, node.id ) # was node.id
        #if (node.id in PageCache):
        if (cachekey in PageCache):
            return PageCache[cachekey]
        else:
            return None

    def AddCachedText(self, node, textStrings, layers='core'):
        """Cache text of our page for this node via its node.id.

        We can be passed a text string or an array of text strings.
        """
        global PageCache
        cachekey = "%s:%s" % ( layers, node.id ) # was node.id
        outputText = "".join(textStrings)
        log.debug("CACHING: %s" % node.id)
        PageCache[cachekey] = outputText
        return outputText

    def write(self, str):
        """Write some text to Web server's output stream."""
        self.outputStrings.append(str)


    def moreInfoBlock(self, node, layer='core'):

        # if we think we have more info on this term, show a bulleted list of extra items.

        # defaults
        bugs = ["No known open issues."]
        mappings = ["No recorded schema mappings."]
        items = bugs + mappings

        items = [

         "<a href='https://github.com/schemaorg/schemaorg/issues?q=is%3Aissue+is%3Aopen+{0}'>Check for open issues.</a>".format(node.id)
        ]

        for l in all_terms[node.id]:
            l = l.replace("#","")
            if ENABLE_HOSTED_EXTENSIONS:
                items.append("'{0}' is mentioned in extension layer: <a href='?ext={1}'>{2}</a>".format( node.id, l, l ))

        moreinfo = """<div>
        <div id='infobox' style='text-align: right;'><b><span style="cursor: pointer;">[more...]</span></b></div>
        <div id='infomsg' style='display: none; background-color: #EEEEEE; text-align: left; padding: 0.5em;'>
        <ul>"""

        for i in items:
            moreinfo += "<li>%s</li>" % i

#          <li>mappings to other terms.</li>
#          <li>or links to open issues.</li>

        moreinfo += """</ul>
          </div>
        </div</div>
        <script type="text/javascript">
        $("#infobox").click(function(x) {
            element = $("#infomsg");
            if (! $(element).is(":visible")) {
                $("#infomsg").show(300);
            } else {
                $("#infomsg").hide(300);

            }
        });
</script>"""
        return moreinfo

    def GetParentStack(self, node, layers='core'):
        """Returns a hiearchical structured used for site breadcrumbs."""
        if (node not in self.parentStack):
            self.parentStack.append(node)

        if (Unit.isAttribute(node, layers=layers)):
            self.parentStack.append(Unit.GetUnit("Property"))
            self.parentStack.append(Unit.GetUnit("Thing"))

        sc = Unit.GetUnit("rdfs:subClassOf")
        if GetTargets(sc, node, layers=layers):
            for p in GetTargets(sc, node, layers=layers):
                self.GetParentStack(p, layers=layers)
        else:
            # Enumerations are classes that have no declared subclasses
            sc = Unit.GetUnit("typeOf")
            for p in GetTargets(sc, node, layers=layers):
                self.GetParentStack(p, layers=layers)

    def ml(self, node, label='', title='', prop='', hashorslash='/'):
        """ml ('make link')
        Returns an HTML-formatted link to the class or property URL

        * label = optional anchor text label for the link
        * title = optional title attribute on the link
        * prop = an optional property value to apply to the A element
        """

        if label=='':
          label = node.id
        if title != '':
          title = " title=\"%s\"" % (title)
        if prop:
            prop = " property=\"%s\"" % (prop)
        return "<a href=\"%s%s\"%s%s>%s</a>" % (hashorslash, node.id, prop, title, label)

    def makeLinksFromArray(self, nodearray, tooltip=''):
        """Make a comma separate list of links via ml() function.

        * tooltip - optional text to use as title of all links
        """
        hyperlinks = []
        for f in nodearray:
           hyperlinks.append(self.ml(f, f.id, tooltip))
        return (", ".join(hyperlinks))

    def emitUnitHeaders(self, node, layers='core'):
        """Write out the HTML page headers for this node."""
        self.write("<h1 class=\"page-title\">\n")
        ind = len(self.parentStack)
        thing_seen = False
        while (ind > 0) :
            ind = ind -1
            nn = self.parentStack[ind]
            if (nn.id == "Thing" or thing_seen or nn.isDataType(layers=layers)):
                thing_seen = True
                self.write(self.ml(nn) )
                if ind == 1 and node.isEnumerationValue(layers=layers):
                    self.write(" :: ")
                elif ind > 0:
                    self.write(" &gt; ")
                if ind == 1:
                    self.write("<span property=\"rdfs:label\">")
                if ind == 0:
                    self.write("</span>")
        self.write("</h1>")
        comment = GetComment(node, layers)
        self.write(" <div property=\"rdfs:comment\">%s</div>\n\n" % (comment) + "\n")

        self.write(" <br><div>Usage: %s</div>\n\n" % (node.UsageStr()) + "\n")

        self.write(self.moreInfoBlock(node))

        if (node.isClass(layers=layers) and not node.isDataType(layers=layers)):

            self.write("<table class=\"definition-table\">\n        <thead>\n  <tr><th>Property</th><th>Expected Type</th><th>Description</th>               \n  </tr>\n  </thead>\n\n")


    def emitSimplePropertiesPerType(self, cl, layers="core", out=None, hashorslash="/"):
        """Emits a simple list of properties applicable to the specified type."""

        if not out:
            out = self

        out.write("<ul class='props4type'>")
        for prop in sorted(GetSources(  Unit.GetUnit("domainIncludes"), cl, layers=layers), key=lambda u: u.id):
            if (prop.superseded(layers=layers)):
                continue
            out.write("<li><a href='%s%s'>%s</a></li>" % ( hashorslash, prop.id, prop.id  ))
        out.write("</ul>\n\n")

    def emitSimplePropertiesIntoType(self, cl, layers="core", out=None, hashorslash="/"):
        """Emits a simple list of properties whose values are the specified type."""

        if not out:
            out = self

        out.write("<ul class='props2type'>")
        for prop in sorted(GetSources(  Unit.GetUnit("rangeIncludes"), cl, layers=layers), key=lambda u: u.id):
            if (prop.superseded(layers=layers)):
                continue
            out.write("<li><a href='%s%s'>%s</a></li>" % ( hashorslash, prop.id, prop.id  ))
        out.write("</ul>\n\n")

    def ClassProperties (self, cl, subclass=False, layers="core", out=None, hashorslash="/"):
        """Write out a table of properties for a per-type page."""
        if not out:
            out = self

        headerPrinted = False
        di = Unit.GetUnit("domainIncludes")
        ri = Unit.GetUnit("rangeIncludes")
        for prop in sorted(GetSources(di, cl, layers=layers), key=lambda u: u.id):
            if (prop.superseded(layers=layers)):
                continue
            supersedes = prop.supersedes(layers=layers)
            olderprops = prop.supersedes_all(layers=layers)
            inverseprop = prop.inverseproperty(layers=layers)
            subprops = prop.subproperties(layers=layers)
            superprops = prop.superproperties(layers=layers)
            ranges = GetTargets(ri, prop, layers=layers)
            comment = GetComment(prop, layers=layers)
            if (not headerPrinted):
                class_head = self.ml(cl)
                if subclass:
                    class_head = self.ml(cl, prop="rdfs:subClassOf")
                out.write("<thead class=\"supertype\">\n  <tr>\n    <th class=\"supertype-name\" colspan=\"3\">Properties from %s</th>\n  </tr>\n</thead>\n\n<tbody class=\"supertype\">\n  " % (class_head))
                headerPrinted = True

            out.write("<tr typeof=\"rdfs:Property\" resource=\"http://schema.org/%s\">\n    \n      <th class=\"prop-nam\" scope=\"row\">\n\n<code property=\"rdfs:label\">%s</code>\n    </th>\n " % (prop.id, self.ml(prop)))
            out.write("<td class=\"prop-ect\">\n")
            first_range = True
            for r in ranges:
                if (not first_range):
                    out.write(" or <br/> ")
                first_range = False
                out.write(self.ml(r, prop='rangeIncludes'))
                out.write("&nbsp;")
            out.write("</td>")
            out.write("<td class=\"prop-desc\" property=\"rdfs:comment\">%s" % (comment))
            if (len(olderprops) > 0):
                olderlinks = ", ".join([self.ml(o) for o in olderprops])
                out.write(" Supersedes %s." % olderlinks )
            if (inverseprop != None):
                out.write("<br/> Inverse property: %s." % (self.ml(inverseprop)))

            out.write("</td></tr>")
            subclass = False

        if subclass: # in case the superclass has no defined attributes
            out.write("<meta property=\"rdfs:subClassOf\" content=\"%s\">" % (cl.id))

    def emitClassIncomingProperties (self, cl, layers="core", out=None, hashorslash="/"):
        """Write out a table of incoming properties for a per-type page."""
        if not out:
            out = self

        headerPrinted = False
        di = Unit.GetUnit("domainIncludes")
        ri = Unit.GetUnit("rangeIncludes")
        for prop in sorted(GetSources(ri, cl, layers=layers), key=lambda u: u.id):
            if (prop.superseded(layers=layers)):
                continue
            supersedes = prop.supersedes(layers=layers)
            inverseprop = prop.inverseproperty(layers=layers)
            subprops = prop.subproperties(layers=layers)
            superprops = prop.superproperties(layers=layers)
            ranges = GetTargets(di, prop, layers=layers)
            comment = GetComment(prop, layers=layers)

            if (not headerPrinted):
                self.write("<br/><br/>Instances of %s may appear as values for the following properties<br/>" % (self.ml(cl)))
                self.write("<table class=\"definition-table\">\n        \n  \n<thead>\n  <tr><th>Property</th><th>On Types</th><th>Description</th>               \n  </tr>\n</thead>\n\n")

                headerPrinted = True

            self.write("<tr>\n<th class=\"prop-nam\" scope=\"row\">\n <code>%s</code>\n</th>\n " % (self.ml(prop)) + "\n")
            self.write("<td class=\"prop-ect\">\n")
            first_range = True
            for r in ranges:
                if (not first_range):
                    self.write(" or<br/> ")
                first_range = False
                self.write(self.ml(r))
                self.write("&nbsp;")
            self.write("</td>")
            self.write("<td class=\"prop-desc\">%s " % (comment))
            if (supersedes != None):
                self.write(" Supersedes %s." % (self.ml(supersedes)))
            if (inverseprop != None):
                self.write("<br/> inverse property: %s." % (self.ml(inverseprop)) )

            self.write("</td></tr>")
        if (headerPrinted):
            self.write("</table>\n")


    def emitRangeTypesForProperty(self, node, layers="core", out=None, hashorslash="/"):
        """Write out simple HTML summary of this property's expected types."""
        if not out:
            out = self

        out.write("<ul class='attrrangesummary'>")
        for rt in sorted(GetTargets(Unit.GetUnit("rangeIncludes"), node, layers=layers), key=lambda u: u.id):
            out.write("<li><a href='%s%s'>%s</a></li>" % ( hashorslash, rt.id, rt.id  ))
        out.write("</ul>\n\n")


    def emitDomainTypesForProperty(self, node, layers="core", out=None, hashorslash="/"):
        """Write out simple HTML summary of types that expect this property."""
        if not out:
            out = self

        out.write("<ul class='attrdomainsummary'>")
        for dt in sorted(GetTargets(Unit.GetUnit("domainIncludes"), node, layers=layers), key=lambda u: u.id):
            out.write("<li><a href='%s%s'>%s</a></li>" % ( hashorslash, dt.id, dt.id  ))
        out.write("</ul>\n\n")



    def emitAttributeProperties(self, node, layers="core", out=None, hashorslash="/"):
        """Write out properties of this property, for a per-property page."""
        if not out:
            out = self

        di = Unit.GetUnit("domainIncludes")
        ri = Unit.GetUnit("rangeIncludes")
        ranges = sorted(GetTargets(ri, node, layers=layers), key=lambda u: u.id)
        domains = sorted(GetTargets(di, node, layers=layers), key=lambda u: u.id)
        first_range = True

        newerprop = node.supersededBy(layers=layers) # None of one. e.g. we're on 'seller'(new) page, we get 'vendor'(old)
        olderprop = node.supersedes(layers=layers) # None or one
        olderprops = node.supersedes_all(layers=layers) # list, e.g. 'seller' has 'vendor', 'merchant'.

        inverseprop = node.inverseproperty(layers=layers)
        subprops = node.subproperties(layers=layers)
        superprops = node.superproperties(layers=layers)


        if (inverseprop != None):
            tt = "This means the same thing, but with the relationship direction reversed."
            out.write("<p>Inverse-property: %s.</p>" % (self.ml(inverseprop, inverseprop.id,tt, prop=False, hashorslash=hashorslash)) )

        out.write("<table class=\"definition-table\">\n")
        out.write("<thead>\n  <tr>\n    <th>Values expected to be one of these types</th>\n  </tr>\n</thead>\n\n  <tr>\n    <td>\n      ")

        for r in ranges:
            if (not first_range):
                out.write("<br/>")
            first_range = False
            tt = "The '%s' property has values that include instances of the '%s' type." % (node.id, r.id)
            out.write(" <code>%s</code> " % (self.ml(r, r.id, tt, prop="rangeIncludes", hashorslash=hashorslash) +"\n"))
        out.write("    </td>\n  </tr>\n</table>\n\n")
        first_domain = True

        out.write("<table class=\"definition-table\">\n")
        out.write("  <thead>\n    <tr>\n      <th>Used on these types</th>\n    </tr>\n</thead>\n<tr>\n  <td>")
        for d in domains:
            if (not first_domain):
                out.write("<br/>")
            first_domain = False
            tt = "The '%s' property is used on the '%s' type." % (node.id, d.id)
            out.write("\n    <code>%s</code> " % (self.ml(d, d.id, tt, prop="domainIncludes",hashorslash=hashorslash)+"\n" ))
        out.write("      </td>\n    </tr>\n</table>\n\n")

        if (subprops != None and len(subprops) > 0):
            out.write("<table class=\"definition-table\">\n")
            out.write("  <thead>\n    <tr>\n      <th>Sub-properties</th>\n    </tr>\n</thead>\n")
            for sbp in subprops:
                c = GetComment(sbp,layers=layers)
                tt = "%s: ''%s''" % ( sbp.id, c)
                out.write("\n    <tr><td><code>%s</code></td></tr>\n" % (self.ml(sbp, sbp.id, tt, hashorslash=hashorslash)))
            out.write("\n</table>\n\n")

        # Super-properties
        if (superprops != None and  len(superprops) > 0):
            out.write("<table class=\"definition-table\">\n")
            out.write("  <thead>\n    <tr>\n      <th>Super-properties</th>\n    </tr>\n</thead>\n")
            for spp in superprops:
                c = GetComment(spp, layers=layers)           # markup needs to be stripped from c, e.g. see 'logo', 'photo'
                c = re.sub(r'<[^>]*>', '', c) # This is not a sanitizer, we trust our input.
                tt = "%s: ''%s''" % ( spp.id, c)
                out.write("\n    <tr><td><code>%s</code></td></tr>\n" % (self.ml(spp, spp.id, tt,hashorslash)))
            out.write("\n</table>\n\n")

        # Supersedes
        if (olderprops != None and len(olderprops) > 0):
            out.write("<table class=\"definition-table\">\n")
            out.write("  <thead>\n    <tr>\n      <th>Supersedes</th>\n    </tr>\n</thead>\n")

            for o in olderprops:
                c = GetComment(o, layers=layers)
                tt = "%s: ''%s''" % ( o.id, c)
                out.write("\n    <tr><td><code>%s</code></td></tr>\n" % (self.ml(o, o.id, tt, hashorslash)))
            out.write("\n</table>\n\n")

        # supersededBy (at most one direct successor)
        if (newerprop != None):
            out.write("<table class=\"definition-table\">\n")
            out.write("  <thead>\n    <tr>\n      <th><a href=\"/supersededBy\">supersededBy</a></th>\n    </tr>\n</thead>\n")
            out.write("\n    <tr><td><code>%s</code></td></tr>\n" % (self.ml(newerprop, newerprop.id, tt,hashorslash)))
            out.write("\n</table>\n\n")

    def rep(self, markup):
        """Replace < and > with HTML escape chars."""
        m1 = re.sub("<", "&lt;", markup)
        m2 = re.sub(">", "&gt;", m1)
        # TODO: Ampersand? Check usage with examples.
        return m2

    def handleHomepage(self, node):
        """Send the homepage, or if no HTML accept header received and JSON-LD was requested, send JSON-LD context file.

        typical browser accept list: ('Accept', 'text/html,application/xhtml+xml,application/xml;q=0.9,image/webp,*/*;q=0.8')
        # e.g. curl -H "Accept: application/ld+json" http://localhost:8080/
        see also http://www.w3.org/Protocols/rfc2616/rfc2616-sec14.html
        https://github.com/rvguha/schemaorg/issues/5
        https://github.com/rvguha/schemaorg/wiki/JsonLd
        """
        accept_header = self.request.headers.get('Accept').split(',')
        logging.info("accepts: %s" % self.request.headers.get('Accept'))

        if ENABLE_JSONLD_CONTEXT:
            jsonldcontext = GetJsonLdContext()

        # Homepage is content-negotiated. HTML or JSON-LD.
        mimereq = {}
        for ah in accept_header:
            ah = re.sub( r";q=\d?\.\d+", '', ah).rstrip()
            mimereq[ah] = 1

        html_score = mimereq.get('text/html', 5)
        xhtml_score = mimereq.get('application/xhtml+xml', 5)
        jsonld_score = mimereq.get('application/ld+json', 10)
        # print "accept_header: " + str(accept_header) + " mimereq: "+str(mimereq) + "Scores H:{0} XH:{1} J:{2} ".format(html_score,xhtml_score,jsonld_score)

        if (ENABLE_JSONLD_CONTEXT and (jsonld_score < html_score and jsonld_score < xhtml_score)):
            self.response.headers['Content-Type'] = "application/ld+json"
            self.emitCacheHeaders()
            self.response.out.write( jsonldcontext )
            return True
        else:
            # Serve a homepage from template
            # the .tpl has responsibility for extension homepages
            # TODO: pass in extension, base_domain etc.
            hp = DataCache.get("homepage")
            if hp != None:
                self.response.out.write( hp )
                log.debug("Served datacache homepage.tpl")
            else:
                template = JINJA_ENVIRONMENT.get_template('homepage.tpl')
                template_values = {
                    'ENABLE_HOSTED_EXTENSIONS': ENABLE_HOSTED_EXTENSIONS,
                    'SCHEMA_VERSION': SCHEMA_VERSION,
                    'myhost': myhost,
                    'mybasehost': mybasehost,
                    'host_ext': host_ext,
                    'debugging': debugging
                }
                page = template.render(template_values)
                self.response.out.write( page )
                log.debug("Served fresh homepage.tpl")
                DataCache["homepage"] = page
                #            self.response.out.write( open("static/index.html", 'r').read() )
            return True
        log.info("Warning: got here how?")
        return False

    def getExtendedSiteName(self, layers):
        """Returns site name (domain name), informed by the list of active layers."""
        if layers==["core"]:
            return "schema.org"
        if len(layers)==0:
            return "schema.org"
        mylayers = layers
        log.debug("EXT: computing sitename from layer list: %s" %  str(mylayers) )
        return (layers[ len(mylayers)-1 ] + ".schema.org")

    def emitSchemaorgHeaders(self, entry='', is_class=False, ext_mappings='', sitemode="default", sitename="schema.org"):
        """
        Generates, caches and emits HTML headers for class, property and enumeration pages. Leaves <body> open.

        * entry = name of the class or property
        """

        rdfs_type = 'rdfs:Property'
        if is_class:
            rdfs_type = 'rdfs:Class'

        generated_page_id = "genericTermPageHeader-%s" % str(entry)
        gtp = DataCache.get( generated_page_id )

        if gtp != None:
            self.response.out.write( gtp )
            log.debug("Served recycled genericTermPageHeader.tpl for %s" % generated_page_id )
        else:
            template = JINJA_ENVIRONMENT.get_template('genericTermPageHeader.tpl')
            template_values = {
                'entry': str(entry),
                'sitemode': sitemode,
                'sitename': sitename,
                'rdfs_type': rdfs_type,
                'ext_mappings': ext_mappings
            }
            out = template.render(template_values)
            DataCache[ generated_page_id ] = out
            log.debug("Served and cached fresh genericTermPageHeader.tpl for %s" % generated_page_id )

            self.response.write(out)


    def emitExactTermPage(self, node, layers="core"):
        """Emit a Web page that exactly matches this node."""
        log.debug("EXACT PAGE: %s" % node.id)
        self.outputStrings = [] # blank slate
        ext_mappings = GetExtMappingsRDFa(node, layers=layers)

        global sitemode, sitename

        if ("schema.org" not in self.request.host and sitemode == "mainsite"):
            sitemode = "mainsite testsite"

        self.emitSchemaorgHeaders(node.id, node.isClass(), ext_mappings, sitemode, sitename)

        if ( ENABLE_HOSTED_EXTENSIONS and ("core" not in layers or len(layers)>1) ):
            ll = " ".join(layers).replace("core","")

            s = "<p id='lli' class='layerinfo %s'><a href=\"https://github.com/schemaorg/schemaorg/wiki/ExtensionList\">extensions shown</a>: %s [<a href='http://%s/'>x</a>]</p>\n" % (ll, ll, mybasehost )
            self.write(s)

        cached = self.GetCachedText(node, layers)
        if (cached != None):
            self.response.write(cached)
            return

        self.parentStack = []
        self.GetParentStack(node, layers=layers)

        self.emitUnitHeaders(node,  layers=layers) # writes <h1><table>...

        if (node.isClass(layers=layers)):
            subclass = True
            for p in self.parentStack:
                self.ClassProperties(p, p==self.parentStack[0], layers=layers)
            self.write("</table>\n")
            self.emitClassIncomingProperties(node, layers=layers)
        elif (Unit.isAttribute(node, layers=layers)):
            self.emitAttributeProperties(node, layers=layers)

        if (not Unit.isAttribute(node, layers=layers)):
            self.write("\n\n</table>\n\n") # no supertype table for properties

        if (node.isClass(layers=layers)):
            children = sorted(GetSources(Unit.GetUnit("rdfs:subClassOf"), node, layers=layers), key=lambda u: u.id)
            if (len(children) > 0):
                self.write("<br/><b>More specific Types</b>");
                for c in children:
                    self.write("<li> %s" % (self.ml(c)))

        if (node.isEnumeration(layers=layers)):
            children = sorted(GetSources(Unit.GetUnit("typeOf"), node, layers=layers), key=lambda u: u.id)
            if (len(children) > 0):
                self.write("<br/><br/>Enumeration members");
                for c in children:
                    self.write("<li> %s" % (self.ml(c)))

        ackorgs = GetTargets(Unit.GetUnit("dc:source"), node, layers=layers)
        if (len(ackorgs) > 0):
            self.write("<h4  id=\"acks\">Acknowledgements</h4>\n")
            for ao in ackorgs:
                acks = sorted(GetTargets(Unit.GetUnit("rdfs:comment"), ao, layers))
                for ack in acks:
                    self.write(str(ack+"<br/>"))

        examples = GetExamples(node, layers=layers)
        if (len(examples) > 0):
            example_labels = [
              ('Without Markup', 'original_html', 'selected'),
              ('Microdata', 'microdata', ''),
              ('RDFa', 'rdfa', ''),
              ('JSON-LD', 'jsonld', ''),
            ]
            self.write("<br/><br/><b><a id=\"examples\">Examples</a></b><br/><br/>\n\n")
            for ex in examples:
                if "id" in ex.egmeta:
                    self.write('<span id="%s"></span>' % ex.egmeta["id"])
                self.write("<div class='ds-selector-tabs ds-selector'>\n")
                self.write("  <div class='selectors'>\n")
                for label, example_type, selected in example_labels:
                    self.write("    <a value='%s' data-selects='%s' class='%s'>%s</a>\n"
                               % (example_type, example_type, selected, label))
                self.write("</div>\n\n")
                for label, example_type, selected in example_labels:
                    self.write("<pre class=\"prettyprint lang-html linenums %s %s\">%s</pre>\n\n"
                               % (example_type, selected, self.rep(ex.get(example_type))))
                self.write("</div>\n\n")

        self.write("<p class=\"version\"><b>Schema Version %s</b></p>\n\n" % SCHEMA_VERSION)
        # TODO: add some version info regarding the extension

        # Analytics
	self.write("""<script>(function(i,s,o,g,r,a,m){i['GoogleAnalyticsObject']=r;i[r]=i[r]||function(){
	  (i[r].q=i[r].q||[]).push(arguments)},i[r].l=1*new Date();a=s.createElement(o),
	  m=s.getElementsByTagName(o)[0];a.async=1;a.src=g;m.parentNode.insertBefore(a,m)
	  })(window,document,'script','//www.google-analytics.com/analytics.js','ga');
	  ga('create', 'UA-52672119-1', 'auto');ga('send', 'pageview');</script>""")

        self.write(" \n\n</div>\n</body>\n</html>")

        self.response.write(self.AddCachedText(node, self.outputStrings, layers))

    def emitHTTPHeaders(self, node):
        if ENABLE_CORS:
            self.response.headers.add_header("Access-Control-Allow-Origin", "*") # entire site is public.
            # see http://en.wikipedia.org/wiki/Cross-origin_resource_sharing

    def handleHTTPRedirection(self, node):
        return False # none yet.
        # https://github.com/schemaorg/schemaorg/issues/4

    def setupExtensionLayerlist(self, node):
        # Identify which extension layer(s) are requested
        # TODO: add subdomain support e.g. bib.schema.org/Globe
        # instead of Globe?ext=bib which is more for debugging.

        # 1. get a comma list from ?ext=foo,bar URL notation
        extlist = cleanPath( self.request.get("ext")  )# for debugging
        extlist = re.sub(ext_re, '', extlist).split(',')
        log.debug("?ext= extension list: %s " % ", ".join(extlist))

        # 2. Ignore ?ext=, start with 'core' only.
        layerlist = [ "core"]

        # 3. Use host_ext if set, e.g. 'bib' from bib.schema.org
        if host_ext != None:
            log.debug("Host: %s host_ext: %s" % ( self.request.host , host_ext ) )
            extlist.append(host_ext)

        # Report domain-requested extensions
        for x in extlist:
            log.debug("Ext filter found: %s" % str(x))
            if x  in ["core", "localhost", ""]:
                continue
            layerlist.append("%s" % str(x))
        layerlist = list(set(layerlist))   # dedup
        log.debug("layerlist: %s" % layerlist)
        return layerlist

    def handleJSONContext(self, node):
        """Handle JSON-LD Context non-homepage requests (including refuse if not enabled)."""

        if not ENABLE_JSONLD_CONTEXT:
            self.error(404)
            self.response.out.write('<title>404 Not Found.</title><a href="/">404 Not Found (JSON-LD Context not enabled.)</a><br/><br/>')
            return True
        if (node=="docs/jsonldcontext.json.txt"):
            jsonldcontext = GetJsonLdContext()
            self.response.headers['Content-Type'] = "text/plain"
            self.emitCacheHeaders()
            self.response.out.write( jsonldcontext )
            return True
        if (node=="docs/jsonldcontext.json"):
            jsonldcontext = GetJsonLdContext()
            self.response.headers['Content-Type'] = "application/ld+json"
            self.emitCacheHeaders()
            self.response.out.write( jsonldcontext )
            return True
        return False
        # see also handleHomepage for conneg'd version.

    def handleFullHierarchyPage(self, node,  layerlist='core'):
        self.response.headers['Content-Type'] = "text/html"
        self.emitCacheHeaders()

        if DataCache.get('FullTreePage'):
            self.response.out.write( DataCache.get('FullTreePage') )
            log.debug("Serving recycled FullTreePage.")
            return True
        else:
            template = JINJA_ENVIRONMENT.get_template('full.tpl')
            uThing = Unit.GetUnit("Thing")
            uDataType = Unit.GetUnit("DataType")

            mainroot = TypeHierarchyTree()
            mainroot.traverseForHTML(uThing, layers=layerlist)
            thing_tree = mainroot.toHTML()

            dtroot = TypeHierarchyTree()
            dtroot.traverseForHTML(uDataType, layers=layerlist)
            datatype_tree = dtroot.toHTML()
            page = template.render({ 'thing_tree': thing_tree, 'datatype_tree': datatype_tree })

            self.response.out.write( page )
            log.debug("Serving fresh FullTreePage.")
            DataCache["FullTreePage"] = page

            return True

    def handleJSONSchemaTree(self, node, layerlist='core'):
        """Handle a request for a JSON-LD tree representation of the schemas (RDFS-based)."""

        self.response.headers['Content-Type'] = "application/ld+json"
        self.emitCacheHeaders()

        if DataCache.get('JSONLDThingTree'):
            self.response.out.write( DataCache.get('JSONLDThingTree') )
            log.debug("Serving recycled JSONLDThingTree.")
            return True
        else:
            uThing = Unit.GetUnit("Thing")
            mainroot = TypeHierarchyTree()
            mainroot.traverseForJSONLD(Unit.GetUnit("Thing"), layers=layerlist)
            thing_tree = mainroot.toJSON()
            self.response.out.write( thing_tree )
            log.debug("Serving fresh JSONLDThingTree.")
            DataCache["JSONLDThingTree"] = thing_tree
            return True
        return False


    def handleExactTermPage(self, node, layers='core'):
        """Handle with requests for specific terms like /Person, /fooBar. """

        #self.outputStrings = [] # blank slate
        schema_node = Unit.GetUnit(node) # e.g. "Person", "CreativeWork".

        if inLayer(layers, schema_node):
            self.emitExactTermPage(schema_node, layers=layers)
            return True
        else:
            # log.info("Looking for node: %s in layers: %s" % (node.id, ",".join(all_layers.keys() )) )
            if not ENABLE_HOSTED_EXTENSIONS:
                return False
            if schema_node is not None and schema_node.id in all_terms:# look for it in other layers
                log.debug("TODO: layer toc: %s" % all_terms[schema_node.id] )
                # self.response.out.write("Layers should be listed here. %s " %  all_terms[node.id] )

                self.response.out.write("<h3>Schema.org Extensions</h3>\n<p>The term '%s' is not in the schema.org core, but is described by the following extension(s):</p>\n<ul>\n" % schema_node.id)
                for x in all_terms[schema_node.id]:
                    x = x.replace("#","")
                    self.response.out.write("<li><a href='?ext=%s'>%s</a></li>" % (x, x) )
                return True
            return False

    def handle404Failure(self, node, layers="core"):
        self.error(404)
        self.emitSchemaorgHeaders("404 Missing")
        self.response.out.write('<h3>404 Not Found.</h3><p><br/>Page not found. Please <a href="/">try the homepage.</a><br/><br/></p>')


        clean_node = cleanPath(node)

        log.debug("404: clean_node: clean_node: %s node: %s" % (clean_node, node))

        base_term = Unit.GetUnit( node.rsplit('/')[0] )
        if base_term != None :
            self.response.out.write('<div>Perhaps you meant: <a href="/%s">%s</a></div> <br/><br/> ' % ( base_term.id, base_term.id ))

        base_actionprop = Unit.GetUnit( node.rsplit('-')[0] )
        if base_actionprop != None :
            self.response.out.write('<div>Looking for an <a href="/Action">Action</a>-related property? Note that xyz-input and xyz-output have <a href="/docs/actions.html">special meaning</a>. See also: <a href="/%s">%s</a></div> <br/><br/> ' % ( base_actionprop.id, base_actionprop.id ))

        return True

    def handleJSONSchemaTree(self, node, layerlist='core'):
        """Handle a request for a JSON-LD tree representation of the schemas (RDFS-based)."""

        self.response.headers['Content-Type'] = "application/ld+json"
        self.emitCacheHeaders()

        if DataCache.get('JSONLDThingTree'):
            self.response.out.write( DataCache.get('JSONLDThingTree') )
            log.debug("Serving recycled JSONLDThingTree.")
            return True
        else:
            uThing = Unit.GetUnit("Thing")
            mainroot = TypeHierarchyTree()
            mainroot.traverseForJSONLD(Unit.GetUnit("Thing"), layers=layerlist)
            thing_tree = mainroot.toJSON()
            self.response.out.write( thing_tree )
            log.debug("Serving fresh JSONLDThingTree.")
            DataCache["JSONLDThingTree"] = thing_tree
            return True
        return False



    # if (node == "version/2.0/" or node == "version/latest/" or "version/" in node) ...

    def handleFullReleasePage(self, node,  layerlist='core'):

        """Deal with a request for a full release summary page. Lists all terms and their descriptions inline in one long page.
        version/latest/ is from current schemas, others will need to be loaded and emitted from stored HTML snapshots (for now)."""

        # http://jinja.pocoo.org/docs/dev/templates/

        global releaselog
        clean_node = cleanPath(node)
        self.response.headers['Content-Type'] = "text/html"
        self.emitCacheHeaders()

        requested_version = clean_node.rsplit('/')[1]
        requested_format = clean_node.rsplit('/')[-1]
        if len( clean_node.rsplit('/') ) == 2:
            requested_format=""

        log.info("Full release page for: node: '%s' cleannode: '%s' requested_version: '%s' requested_format: '%s' l: %s" % (node, clean_node, requested_version, requested_format, len(clean_node.rsplit('/')) ) )

        # Full release page for: node: 'version/' cleannode: 'version/' requested_version: '' requested_format: '' l: 2
        # /version/
        if (clean_node=="version/" or clean_node=="version") and requested_version=="" and requested_format=="":
            log.info("Table of contents should be sent instead, then succeed.")
            if DataCache.get('tocVersionPage'):
                self.response.out.write( DataCache.get('tocVersionPage'))
                return True
            else:
                template = JINJA_ENVIRONMENT.get_template('tocVersionPage.tpl')
                page = template.render({ "releases": releaselog.keys() })

                self.response.out.write( page )
                log.debug("Serving fresh tocVersionPage.")
                DataCache["tocVersionPage"] = page
                return True

        if requested_version in releaselog:
            log.info("Version '%s' was released on %s. Serving from filesystem." % ( node, releaselog[requested_version] ))

            version_rdfa = "data/releases/%s/schema.rdfa" % requested_version
            version_allhtml = "data/releases/%s/schema-all.html" % requested_version
            version_nt = "data/releases/%s/schema.nt" % requested_version

            if requested_format=="":
                #self.response.out.write( open(version_allhtml, 'r').read() )
                #return True
                log.info("Skipping filesystem for now.")

            if requested_format=="schema.rdfa":
                self.response.headers['Content-Type'] = "application/octet-stream" # It is HTML but ... not really.
                self.response.headers['Content-Disposition']= "attachment; filename=schemaorg_%s.rdfa.html" % requested_version
                self.response.out.write( open(version_rdfa, 'r').read() )
                return True

            if requested_format=="schema.nt":
                self.response.headers['Content-Type'] = "application/n-triples" # It is HTML but ... not really.
                self.response.headers['Content-Disposition']= "attachment; filename=schemaorg_%s.rdfa.nt" % requested_version
                self.response.out.write( open(version_nt, 'r').read() )
                return True

            if requested_format != "":
                return False # Turtle, csv etc.

        else:
            log.info("Unreleased version requested. We only understand requests for latest if unreleased.")

            if requested_version != "latest":
                return False
                log.info("giving up to 404.")
            else:
                log.info("generating a live view of this latest release.")


        if DataCache.get('FullReleasePage'):
            self.response.out.write( DataCache.get('FullReleasePage') )
            log.debug("Serving recycled FullReleasePage.")
            return True
        else:
            template = JINJA_ENVIRONMENT.get_template('fullReleasePage.tpl')
            mainroot = TypeHierarchyTree()
            mainroot.traverseForHTML(Unit.GetUnit("Thing"), hashorslash="#term_", layers=layerlist)
            thing_tree = mainroot.toHTML()
            base_href = "/version/%s/" % requested_version

            az_types = GetAllTypes()
            az_types.sort( key=lambda u: u.id)
            az_type_meta = {}

            az_props = GetAllProperties()
            az_props.sort( key = lambda u: u.id)
            az_prop_meta = {}


#TODO: ClassProperties (self, cl, subclass=False, layers="core", out=None, hashorslash="/"):

            # TYPES
            for t in az_types:
                props4type = HTMLOutput() # properties applicable for a type
                props2type = HTMLOutput() # properties that go into a type

                self.emitSimplePropertiesPerType(t, out=props4type, hashorslash="#term_" )
                self.emitSimplePropertiesIntoType(t, out=props2type, hashorslash="#term_" )

                #self.ClassProperties(t, out=typeInfo, hashorslash="#term_" )
                tcmt = Markup(GetComment(t))
                az_type_meta[t]={}
                az_type_meta[t]['comment'] = tcmt
                az_type_meta[t]['props4type'] = props4type.toHTML()
                az_type_meta[t]['props2type'] = props2type.toHTML()

            # PROPERTIES
            for pt in az_props:
                attrInfo = HTMLOutput()
                rangeList = HTMLOutput()
                domainList = HTMLOutput()
                # self.emitAttributeProperties(pt, out=attrInfo, hashorslash="#term_" )
                # self.emitSimpleAttributeProperties(pt, out=rangedomainInfo, hashorslash="#term_" )

                self.emitRangeTypesForProperty(pt, out=rangeList, hashorslash="#term_" )
                self.emitDomainTypesForProperty(pt, out=domainList, hashorslash="#term_" )

                cmt = Markup(GetComment(pt))
                az_prop_meta[pt] = {}
                az_prop_meta[pt]['comment'] = cmt
                az_prop_meta[pt]['attrinfo'] = attrInfo.toHTML()
                az_prop_meta[pt]['rangelist'] = rangeList.toHTML()
                az_prop_meta[pt]['domainlist'] = domainList.toHTML()

            page = template.render({ "base_href": base_href, 'thing_tree': thing_tree,
                    'liveversion': SCHEMA_VERSION,
                    'requested_version': requested_version,
                    'releasedate': releaselog[str(SCHEMA_VERSION)],
                    'az_props': az_props, 'az_types': az_types,
                    'az_prop_meta': az_prop_meta, 'az_type_meta': az_type_meta })

            self.response.out.write( page )
            log.debug("Serving fresh FullReleasePage.")
            DataCache["FullReleasePage"] = page
            return True


    def setupHostinfo(self, node):
        global debugging, host_ext, myhost, mybasehost

        host_ext = re.match( r'([\w\-_]+)[\.:]?', self.request.host).group(1)
        log.debug("setupHostinfo: srh=%s host_ext=%s" % (self.request.host, str(host_ext) ))

        if host_ext != None:
            # e.g. "bib"
            log.debug("HOST: Found %s in %s" % ( host_ext, self.request.host ))
            myhost = self.request.host.rsplit(':')[0]
            mybasehost = myhost
            mybasehost = mybasehost.replace(host_ext + ".","")
            # mybasehost = mybasehost.replace(":8080", "")


        if "localhost" in self.request.host or "sdo-gozer.appspot.com" in self.request.host:
            debugging = True

    def get(self, node):

        """Get a schema.org site page generated for this node/term.

        Web content is written directly via self.response.

        CORS enabled all URLs - we assume site entirely public.
        See http://en.wikipedia.org/wiki/Cross-origin_resource_sharing

        These should give a JSON version of schema.org:

            curl --verbose -H "Accept: application/ld+json" http://localhost:8080/docs/jsonldcontext.json
            curl --verbose -H "Accept: application/ld+json" http://localhost:8080/docs/jsonldcontext.json.txt
            curl --verbose -H "Accept: application/ld+json" http://localhost:8080/

        Per-term pages vary for type, property and enumeration.

        Last resort is a 404 error if we do not exactly match a term's id.

        See also https://webapp-improved.appspot.com/guide/request.html#guide-request
        """

        global debugging, host_ext, myhost, mybasehost, sitename

        self.setupHostinfo(node)

        self.emitHTTPHeaders(node)

        if self.handleHTTPRedirection(node):
            return

        if (node in silent_skip_list):
            return

        if ENABLE_HOSTED_EXTENSIONS:
            layerlist = self.setupExtensionLayerlist(node) # e.g. ['core', 'bib']
        else:
            layerlist = ["core"]

        sitename = self.getExtendedSiteName(layerlist) # e.g. 'bib.schema.org', 'schema.org'

        log.debug("EXT: set sitename to %s " % sitename)
        if (node in ["", "/"]):
            if self.handleHomepage(node):
                return
            else:
                log.info("Error handling homepage: %s" % node)
                return

        if node in ["docs/jsonldcontext.json.txt", "docs/jsonldcontext.json"]:
            if self.handleJSONContext(node):
                return
            else:
                log.info("Error handling JSON-LD context: %s" % node)
                return

        if (node == "docs/full.html"): # DataCache.getDataCache.get
            if self.handleFullHierarchyPage(node, layerlist=layerlist):
                return
            else:
                log.info("Error handling full.html : %s " % node)
                return

        if (node == "docs/tree.jsonld" or node == "docs/tree.json"):
            if self.handleJSONSchemaTree(node, layerlist=layerlist):
                return
            else:
                log.info("Error handling JSON-LD schema tree: %s " % node)
                return

        if (node == "version/2.0/" or node == "version/latest/" or "version/" in node):
            if self.handleFullReleasePage(node, layerlist=layerlist):
                return
            else:
                log.info("Error handling full release page: %s " % node)
                if self.handle404Failure(node):
                    return
                else:
                    log.info("Error handling 404 under /version/")
                    return

        # Pages based on request path matching a Unit in the term graph:
        if self.handleExactTermPage(node, layers=layerlist):
            return
        else:
            log.info("Error handling exact term page. Assuming a 404: %s" % node)

            # Drop through to 404 as default exit.
            if self.handle404Failure(node):
                return
            else:
                log.info("Error handling 404.")
                return


#log.info("STARTING UP... reading schemas.")
read_schemas(loadExtensions=ENABLE_HOSTED_EXTENSIONS)
schemasInitialized = True

app = ndb.toplevel(webapp2.WSGIApplication([("/(.*)", ShowUnit)]))
