# -*- coding: utf-8 -*-
import os
import datetime
import cPickle as pickle
import re
import subprocess
from utils.db import downstream
from utils.types import Repo, DownstreamRelease

distro_id = downstream.distro("gentoo", "", "A source based meta-distribution.", "http://www.gentoo.org")

MIRROR = "rsync.namerica.gentoo.org::gentoo-portage"
PORTAGE = "/usr/portage/"
STORAGE = "files/gentoo/"
CACHE_FN = "cache.pickle"

FUNCTION_PATTERN = re.compile("^[a-z_]+[ \t]*\(\)")
VARIABLE_PATTERN = re.compile("^([A-Z]+|[ \t]*KEYWORDS)=")

MONTHS = {"Jan":"Jan",
					"Feb":"Feb",
					"Mar":"Mar",
					"Apr":"Apr",
					"May":"May",
					"Jun":"Jun",
					"Jul":"Jul",
					"Aug":"Aug",
					"Sep":"Sep",
					"Oct":"Oct",
					"Okt":"Oct",
					"Nov":"Nov",
					"Dec":"Dec"}

# return a list of ["ubuntu", branch, codename, component, arch, None, None]
def get_repos(test):
	if not os.path.exists(PORTAGE+"profiles/arch.list"):
		update_portage()
	repos = []
	f = open(PORTAGE+"profiles/arch.list")
	arches = map(lambda s: s.strip(),f.readlines())
	arches = arches[:arches.index("")]
	f.close()
	for c,b in [("stable","current"),("unstable","future")]:
		for a in arches:
			repo = Repo()
			repo.distro_id = distro_id
			repo.component = c
			repo.codename = ""
			repo.architecture = a
			downstream.repo(repo, test)
			downstream.add_branch(repo, b, test)
			repos.append(repo)
	repo = Repo()
	repo.distro_id = distro_id
	repo.codename = ""
	repo.component = "unknown"
	repo.architecture = "unknown"
	downstream.repo(repo, test)
	downstream.add_branch(repo, "past", test)
	repos.append(repo)
	return repos

def parse(f, debug = False):
	pkg = {}
	this_key = "unknown"
	variable = False
	function = False
	
	for line in f:
		f_match = FUNCTION_PATTERN.match(line)
		v_match = VARIABLE_PATTERN.match(line)
		line = line.strip()
		if line.startswith("#"):							#comments
			if not pkg.has_key("comments"):
				pkg["comments"]=[]
			pkg["comments"].append(line)
		elif v_match:	#variable
			v_match = v_match.group()
			key = v_match.split("=")[0].strip()
			val = line.strip(v_match).split("\"")
			try:
				if line.count("\"")==1:
					this_key = key
					pkg[key] = [val[1]]
				elif line.count("\"")==2:
					if key not in pkg or len(pkg[key]) < len(val[1]):
						pkg[key] = val[1]
				else:
					pkg[key] = val[0]
			except:
				print v_match,val
		elif variable and "\"" in line:
			variable = False
			pkg[this_key].append(line.strip("\""))
			this_key = "unknown"
		elif f_match:	#function
			f_match = f_match.group()
			key = f_match.split("(")[0]
			if "{" in line:
				val = line.split("{")[1].split("}")[0]
			else:
				val = ""
			if "}" not in line:
				this_key = key
				pkg[key] = [val]
			else:
				pkg[key] = val
		elif function and line.strip()=="}":
			function = False
			this_key = "unknown"
		elif line.strip()=="":
			pass
		else:
			if not pkg.has_key(this_key):
				pkg[this_key] = []
			pkg[this_key].append(line.strip())
	return pkg

def crawl_changelog(category,package,last_crawl=None):
	fn = PORTAGE+category+"/"+package+"/ChangeLog"
	try:
		last = os.stat(fn).st_mtime
	except:
		print "WARNING: missing %s"%fn
		return []
	last = datetime.datetime.utcfromtimestamp(last)
	rels = []
	# check last crawl
	if last_crawl==None or last > last_crawl:
		try:
			f = open(fn)
		except:
			print "WARNING: opening %s"%fn
			return []
		
		for line in f:
			if line.startswith("*"+package) and " " in line:
				try :
					version,d,m,y	= line.strip().split()
					version = version.replace("*"+package+"-","").replace(".ebuild","")
					d = d[1:]
					y = y[:-1]
					revision = "0"
					if "-" in version:
						version, revision = version.rsplit("-",1)
						try:
							revision = str(int(revision[1:]))
						except:
							version = version+"_"+revision
							revision = "0"
					if len(m)>3:
						released = datetime.datetime.strptime(" ".join((d,m,y)),"%d %B %Y")
					else:
						released = datetime.datetime.strptime(" ".join((d,MONTHS[m],y)),"%d %b %Y")
					if last_crawl==None or released > last_crawl:
						rel = DownstreamRelease()
						rel.package = package
						rel.version = version
						rel.revision = revision
						rel.released = released
						rels.append(rel)
				except:
					print "WARNING: parsing '%s' in %s"%(line,fn)
		f.close()
	return rels

def update_portage():
	return True

# return a list of [name, version, revision, epoch, time, extra]
def crawl_repo(repo):
	# check the cache
	try:
		last = os.stat(STORAGE+CACHE_FN)
	except OSError:
		print "no cache"
		last = None
	
	if last:
		last = last.st_mtime
		last = datetime.datetime.utcfromtimestamp(last)
		# check to make sure the cache is reasonably new
		if datetime.datetime.now()-last<datetime.timedelta(hours=1):
			#load the cache and return the desired subset
			f = open(STORAGE+CACHE_FN)
			cache = pickle.load(f)
			if cache.has_key("last_crawl"):
				last_crawl = cache["last_crawl"]
			else:
				last_crawl = None
			f.close()
			if cache.has_key(repo.architecture) and cache[repo.architecture].has_key(repo.component):
				return (last_crawl,cache[repo.architecture][repo.component])
			return (last_crawl, [])
	
	# only do this if the cache is old
	
	if not update_portage():
		return (repo.last_crawl,[])
	
	pkgs = {"unknown":{"unknown":[]},"last_crawl":repo.last_crawl}
	dirs = os.listdir(PORTAGE)
	f = open(PORTAGE+"profiles/categories")
	dirs = map(lambda s: s.strip(),f.readlines())
	dirs = filter(lambda x: x!="virtual", dirs)
	f.close()
	for d in dirs:
		packages = os.listdir(PORTAGE+d+"/")
		for p in filter(lambda x: x!="metadata.xml",packages):
			# crawl change log
			pkgs["unknown"]["unknown"] += crawl_changelog(d,p,repo.last_crawl)
			for v in filter(lambda x: x.startswith(p+"-"), os.listdir(PORTAGE+d+"/"+p+"/")):
				fn = PORTAGE+d+"/"+p+"/"+v
				# Its convention to have 9999 in the version of ebuilds that pull directly from the projects vcs. Those aren't releases so we skip them.
				if "9999" in v:
					continue
				last2 = os.stat(fn).st_mtime
				last2 = datetime.datetime.utcfromtimestamp(last2)
				
				if pkgs["last_crawl"]==None or last2 > pkgs["last_crawl"]:
					pkgs["last_crawl"] = last2
				
				# check last crawl
				if repo.last_crawl==None or last2 > repo.last_crawl:
					try:
						f = open(fn)
						pkg = parse(f)
					except:
						print "WARNING: parsing %s"%fn
						continue
					f.close()
					
					# parse v for version and revision
					v_split = v.strip(p+"-").strip(".ebuild").rsplit("-r",1)
					if len(v_split)==1:
						version = v_split[0]
						revision = "0"
					else:
						version,revision = v_split
					
					pkg["filename"] = v
					
					first = True
					if not pkg.has_key("KEYWORDS"):
						print "WARNING: %s has no keywords."%(d+"/"+p+"/"+v)
						continue
					
					for kw in "".join(pkg["KEYWORDS"]).split(" "):
						if kw=="because" or kw=="all" or kw=="dont":
							print "WARNING: " + fn + " parsed keywords wrong"
						if not kw.startswith("-") and "*" not in kw:
							if "~" in kw:
								branch = "unstable"
								arch = kw.strip("~")
							else:
								branch = "stable"
								arch = kw
							
							if not pkgs.has_key(arch):
								pkgs[arch]={}
							
							if not pkgs[arch].has_key(branch):
								pkgs[arch][branch]=[]
							
							rel = DownstreamRelease()
							rel.package = p
							rel.version = version
							rel.revision = revision
							rel.released = last2
							pkgs[arch][branch].append(rel)
	
	# cache it
	f = open(STORAGE+CACHE_FN,"wb")
	pickle.dump(pkgs,f)
	f.close()
	if pkgs.has_key(repo.architecture) and pkgs[repo.architecture].has_key(repo.component):
		return (pkgs["last_crawl"],pkgs[repo.architecture][repo.component])
	else:
		print "not found in cache"
		return (pkgs["last_crawl"],[])
