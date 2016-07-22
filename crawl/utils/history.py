# -*- coding: utf-8 -*-
import psycopg2 as db
import sys
import os
import datetime

sys.path.append(os.getcwd())
from utils import helper
from utils.version import VersionTree
from utils.timeline import *
from utils.db import core
from utils.db import cursor
from utils.errors import *
from utils.version import VersionTree
from utils.db import downstream

HOST, USER, PASSWORD, DB = helper.mysql_settings()

VERBOSE = False
VERBOSE_RESULT = False

class PackageHistory:
	def __init__(self, name, force_approx = False):
		self.name = name
		# find the package name aliases
		with cursor() as cur:
			cur.execute("SELECT id FROM packages WHERE name = %s",(name,))
			result = cur.fetchone()
			if result == None:
				raise UnknownPackageError(name)
			else:
				sid = result[0]
			
			# find the root of the uptree
			sources = [sid]
			while True:
				cur.execute("SELECT package_tgt FROM links WHERE package_src = %s",(sid,))
				row = cur.fetchone()
				if row==None:
					break
				sid = row[0]
				# Detect cycles in the graph.
				if sid in sources:
					raise LinkCycleError(str(sources))
				sources.append(sid)
			
			cur.execute("SELECT name, description FROM packages LEFT OUTER JOIN package_info ON (packages.id = package_info.package_id) WHERE packages.id = %s",(sid,))
			sname, self.description = cur.fetchone()
			
			if VERBOSE:
				print "found real name",sname
			self.name = sname
			
			explore = [sid]
			aliases = [sid]
			while len(explore)>0:
				tmp = []
				for sid in explore:
					cur.execute("SELECT package_src FROM links WHERE package_tgt = %s",(sid,))
					for row in cur:
                                                if row[0] in aliases:
                                                        continue
						aliases.append(row[0])
						tmp.append(row[0])
				explore = tmp
			
			#print "aliases",aliases
			
			distro_aliases = {}
			for alias in aliases:
				cur.execute("SELECT DISTINCT distros.id FROM dreleases, repos, distros WHERE dreleases.repo_id = repos.id AND distros.id = repos.distro_id AND dreleases.package_id = %s",(alias,))
				for row in cur:
					with cursor() as cur2:
						cur2.execute("SELECT * FROM unlinks WHERE package_id = %s AND distro_id = %s",(alias,row[0]))
						if cur2.rowcount == 0:
							if row[0] not in distro_aliases:
								distro_aliases[row[0]] = [alias]
							else:
								distro_aliases[row[0]].append(alias)
			self.aliases = distro_aliases
			if VERBOSE:
				print "distro aliases", self.aliases
			
			self.ish = False
			#print "query upstream"
			if not force_approx:
				q = "SELECT version, MIN(released) FROM ureleases WHERE package_id in ("+ ", ".join(("%s",)*len(aliases)) + ") GROUP BY version ORDER BY MIN(released), version"
				cur.execute(q,aliases)
			if cur.rowcount == 0 or force_approx:
				if VERBOSE:
					print "falling back to approximate upstream"
				self.ish = True
				q = "SELECT dreleases.version, MIN(dreleases.released) FROM dreleases WHERE package_id in ("+ ", ".join(("%s",)*len(aliases)) + ") AND dreleases.version!='9999' GROUP BY dreleases.version ORDER BY MIN(dreleases.released), dreleases.version"
				cur.execute(q,aliases)
			
			data = []
			if VERBOSE:
				print "upstream"
			for version,date in cur:
				if VERBOSE:
					print version,date
				data.append((date, version))
		
		self.timeline = Timeline(data)
		self.count = DayTimeline(data,default=[])
	
	def get_greatest_timeline(self):
		versions = VersionTree(self.timeline)
		greatest = Timeline(default="0")
		for date in self.timeline:
			versions.add_release(date, self.timeline[date])
			greatest[date] = versions.max(greatest[-1], self.timeline[date])
		
		return greatest
	
	def __str__(self):
		if self.ish:
			pre = "approx history of "
		else:
			pre = "history of "
		return "\n".join((pre+self.name,str(self.timeline),"release count",str(self.count)))

class DistroPackageData:
	def __init__(self):
		self.package = None
		self.newest = None
		self.current = None
		self.num_newer = None
		self.lag = None
		self.ish = None
	
	def __str__(self):
		return " ".join(map(str, (self.package, self.ish, self.newest, self.current, self.num_newer, self.lag)))

class DistroHistory:
	def __init__(self, name, packages=[], branch=None, codename=None, arch=None, now=None):
		self.name = name
		self.branch = branch
		self.codename = codename
		
		with cursor() as cur:
			cur.execute("SELECT id, color FROM distros WHERE name = %s",(name,))
			row = cur.fetchone()
			if row==None:
				raise UnknownDistroError(name)
			
			self.id, self.color = row
			
			if branch!=None and codename==None:
				cur.execute("SELECT repos.codename FROM repos, branches WHERE repos.distro_id = %s AND branches.repo_id = repos.id AND branches.branch=%s ORDER BY branches.start DESC LIMIT 1",(self.id, branch))
				row = cur.fetchone()
				if row!=None:
					self.codename = row[0]
		
		self.arch = arch
		self._now = now
		
		# map a package name to a PackageHistory and ConnectedTimeline
		self._packages = {}
		# packages not used to compute distro history
		self._hidden = []
		self._pkg_order = []
		
		if packages == None:
			packages = downstream.list_all_distro_packages(name)
			print len(packages),"packages"
			packages = map(PackageHistory, packages)
		
		for p in packages:
			self.add_pkg(p)
	
	def snapshot(self, date):
		result = []
		for p in self._pkg_order:
			#timeline = self._compute_package_age(self._packages[p][0].timeline, self._packages[p][1])
			timeline = self._compute_package_obsoletion(self._packages[p][0].timeline, self._packages[p][1])
			result.append((self._packages[p][1].last(date),timeline[date]))
		return result
	
	def snapshot_all_metrics(self, date=None):
		result = []
		if date==None:
			date = self._now
		for p in self._pkg_order:
			lag = self._compute_package_age(self._packages[p][0].timeline, self._packages[p][1])
			obs = self._compute_package_obsoletion(self._packages[p][0].timeline, self._packages[p][1])
			# return package name, current version, newer versions, lag
			data = DistroPackageData()
			data.package = p
			data.ish = self._packages[p][0].ish
			upstream = self._packages[p][0].timeline
			data.newest = self._get_greatest_timeline(upstream, upstream).last(date)
			data.current = self._packages[p][1].last(date)
			data.num_newer = obs[date]
			data.lag = lag[date]
			result.append(data)
		return result
	
	def get_lag_timeline(self):
		timeline = ConnectedTimeline(default=timedelta())
		count = StepTimeline()
		for package in self._packages:
			packagehistory, downstream = self._packages[package]
			upstream = packagehistory.timeline
			if len(downstream)>0:
				age = self._compute_package_age(upstream, downstream)
			else:
				age = ConnectedTimeline()
			timeline = timeline + age
			if len(age.keys())>0:
				count = count + StepTimeline([(age.keys()[0],1)])
		
		timeline = timeline / count
		return timeline
	
	def get_obsoletion_timeline(self):
		return self._get_obsoletion_timeline()[1]
		
	def get_obsoletion_count_timeline(self):
		return self._get_obsoletion_timeline()[0]
	
	def _get_obsoletion_timeline(self):
		obs_timeline = StepTimeline(default=0.0)
		bin_obs_timeline = StepTimeline(default=0.0)
		fcount = StepTimeline()
		for package in self._packages:
			packagehistory, downstream = self._packages[package]
			upstream = packagehistory.timeline
			if len(downstream)>0:
				obs = self._compute_package_obsoletion(upstream, downstream)
				bin_obs = StepTimeline()
				for date in obs:
					if obs[date] > 0:
						bin_obs[date] = 1.0
					else:
						bin_obs[date] = 0.0
			else:
				obs = StepTimeline()
				bin_obs = StepTimeline()
			obs_timeline = obs_timeline + obs
			bin_obs_timeline = bin_obs_timeline + bin_obs

			if len(obs.keys())>0:
				fcount = fcount + StepTimeline([(obs.keys()[0],1.0)])
		
		obs_timeline = obs_timeline / fcount
		bin_obs_timeline = bin_obs_timeline / fcount
		return (obs_timeline, bin_obs_timeline)
	
	def get_pkg(self, name):
		return self._packages[name][1]
	
	def add_pkg(self, package):
		upstream = package.timeline
		downstream = self.get_downstream(package)
		downstream = self._get_greatest_timeline(upstream, downstream)
	
		self._packages[package.name] = (package, downstream)
		self._pkg_order.append(package.name)
	
	def get_downstream(self, package, revisions=False):
		if self.id not in package.aliases:
			return Timeline()
		
		releases = core.get_downstream_releases(self.id, package.aliases[self.id], self.branch, revisions)
		
		downstream = Timeline()
		if VERBOSE:
			print "downstream"
		if revisions:
			for version, revision, date in releases:
				if VERBOSE:
					print version, revision, date
				downstream[date] = (version, revision)
		else:
			for version, date in releases:
				if VERBOSE:
					print version, date
				downstream[date] = version

		if VERBOSE:
			print
		return downstream

	def _get_greatest_timeline(self, upstream, downstream):
		versions = VersionTree(upstream)
		greatest = Timeline(default="0")
		for date in downstream:
			version = downstream[date]
			greatest[date] = versions.max(greatest[-1],version)
		
		return greatest

	def _compute_package_age(self, upstream, downstream):
		ms = timedelta(microseconds=1)
		versions = VersionTree()
		age = ConnectedTimeline(default=timedelta())
		greatest_downstream = "0"
		u = 0
		d = 0
		#print
		#print "interleave"
		#interleave the dates
		while u+d < len(upstream)+len(downstream):
			version = None
			date = None
			if u<len(upstream) and (len(downstream)==d or upstream.keys()[u]<=downstream.keys()[d]):
				if VERBOSE:
					print "upstream",upstream.keys()[u],upstream[u]
				date = upstream.keys()[u]
				version = upstream[u]
				versions.add_release(date,version)
				if greatest_downstream != "0":
					age[date] = versions.compute_lag(date, greatest_downstream)
				u+=1
			else:
				version = downstream[d]
				date = downstream.keys()[d]
				if VERBOSE:
					print "downstream", date, version
				
				if VERBOSE:
					print "greatest downstream before:",greatest_downstream, version
				if greatest_downstream != "0":
					age[date] = versions.compute_lag(date, greatest_downstream)
				greatest_downstream = versions.max(greatest_downstream,version)
				if VERBOSE:
					print "greatest downstream after:",greatest_downstream
				age[date+ms] = versions.compute_lag(date, greatest_downstream)
				d+=1
		#print
		#print "age"
		now = self._now
		if now==None:
			now = datetime.now()
		if greatest_downstream!=None:
			age[now] = versions.compute_lag(now, greatest_downstream)
		
		if VERBOSE or VERBOSE_RESULT:
			for a in age:
				print a,age[a]
			print
		return age
	
	def _compute_package_obsoletion(self, upstream, downstream):
		ms = timedelta(microseconds=1)
		versions = VersionTree()
		age = StepTimeline()
		greatest_downstream = "0"
		u = 0
		d = 0
		#print
		#print "interleave"
		#interleave the dates
		while u+d < len(upstream)+len(downstream):
			version = None
			date = None
			if u<len(upstream) and (len(downstream)==d or upstream.keys()[u]<=downstream.keys()[d]):
				if VERBOSE:
					print "upstream",upstream.keys()[u],upstream[u]
				date = upstream.keys()[u]
				version = upstream[u]
				versions.add_release(date,version)
				if greatest_downstream != "0":
					age[date] = versions.compute_obsoletions(date, greatest_downstream)
				u+=1
			else:
				if VERBOSE:
					print "downstream",downstream.keys()[d],downstream[d]
				version = downstream[d]
				date = downstream.keys()[d]
				if VERBOSE:
					print "greatest downstream before:", greatest_downstream, version
				greatest_downstream = versions.max(greatest_downstream,version)
				if VERBOSE:
					print "greatest downstream after:", greatest_downstream
				age[date+ms] = versions.compute_obsoletions(date, greatest_downstream)
				d+=1
		#print
		#print "age"
		now = self._now
		if now==None:
			now = datetime.now()
		if greatest_downstream!=None:
			age[now] = versions.compute_obsoletions(now, greatest_downstream)
		
		if VERBOSE or VERBOSE_RESULT:
			for a in age:
				print a,age[a]
			print
		return age

def get_upstream(history=True):
	with cursor() as cur:
		result = []
		q = cur.execute("SELECT DISTINCT name FROM packages, ureleases WHERE releases.package_id = packages.id")
		total = q
		i = 0
		for pkg in cur:
			#print pkg[0],i,"/",total
			if history:
				result.append(PackageHistory(pkg[0]))
			else:
				result.append(pkg[0])
			i += 1
	return result

def get_all(history=True):
	with cursor() as cur:
		result = []
		q = cur.execute("SELECT name FROM packages")
		total = q
		i = 0
		for pkg in cur:
			#print pkg[0],i,"/",total
			if history:
				result.append(PackageHistory(pkg[0]))
			else:
				result.append(pkg[0])
			i += 1
	return result

def get_all_distros(packages, branch):
	distros = core.get_all_distros()
	return map(lambda name: DistroHistory(name, packages, branch), distros)


if __name__=="__main__":
	if len(sys.argv)<2:
		print sys.argv[0],"<package>","[distro]","[branch]"
		sys.exit(1)
	VERBOSE = True
	p = sys.argv[1]
	d = None
	b = "current"
	
	if len(sys.argv)>2:
		d = sys.argv[2]
	
	if len(sys.argv)>3:
		b = sys.argv[3]

	p = PackageHistory(p)
	print p
	
	vt = VersionTree(p.timeline)
	print vt

	if d != None:
		d = DistroHistory(d,[p],b)
		print "Distro releases:"
		print d.get_pkg(p.name)
		print
		print "Lag:"
		print d.get_lag_timeline()[-3:]
		print
		print "Obsolete:"
		print d.get_obsoletion_count_timeline()[-3:]
		print
