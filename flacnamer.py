#!/usr/bin/python2.5
#
# Script to automatically look up albums in the musicbrainz database and
# rename/retag FLAC files.
# Also gets album art via amazon web services
#
# (C) 2008 Scott Raynel <scottraynel@gmail.com>
#

import sys
import os
from datetime import timedelta
import musicbrainz2.model
import toc
import discid
import shutil
import urllib
import mp3names
import subprocess
import re
import time
import submit #musicbrainz_submission_url()
import musicdns
import lookups
import albumidentify
import albumidentifyconfig
import operator
import tag
import parsemp3
import serialisemp3
import tempfile
import traceback
import datetime
import optparse

string_expandos = ["trackname", "trackartist", "album", "albumartist", "sortalbumartist", "sorttrackartist"]
integer_expandos = ["tracknumber", "year"]

force_short_album = False
report_entries = []
srcpath=None

def report(message):
        ts = datetime.datetime.now().ctime()
        if (type(message) == type([])):
                for m in message:
                        report(m)
                return
        report_entries.append("%s: %s" % (ts, message))
        print message

def write_report(reportpath):
        f = open(reportpath, "w")
        for r in report_entries:
                f.write(r + "\n")
        f.close()

def makedirs(path):
        """ Ensure all directories exist """
        if path == os.sep:
                return
        makedirs(os.path.dirname(path))
        try:
                os.mkdir(path)
        except:
                pass

def get_release_by_fingerprints(disc):
        """ Do a fingerprint based search for a matching release.

        """
        dirinfo = albumidentify.get_dir_info(disc.dirname)

        if len(dirinfo) < 3 and not force_short_album:
                report("Too few tracks to be reliable (%i), use --force-short-album" % len(dirinfo))
                return None

        data = albumidentify.guess_album(dirinfo)
        try:
                (directoryname, albumname, rid, events, asin, trackdata, albumartist, releaseid) = \
                        data.next()
        except StopIteration,si:
                report("No matches from fingerprint search")
		return None

        release = lookups.get_release_by_releaseid(releaseid)
        print "Got result via audio fingerprinting!"

        if disc.tocfilename:
                report("Suggest submitting TOC and discID to musicbrainz:")
                report("Release URL: " + release.id + ".html")
                report("Submit URL : " + submit.musicbrainz_submission_url(disc))

        # When we id by fingerprints, the sorted original filenames may not
        # match the actual tracks (i.e. out of order, bad naming, etc). Here we
        # have identified the release, so we need to remember the actual
        # filename for each track for later.
        sorted(trackdata, key=operator.itemgetter(0)) # sort trackdata by tracknum
        disc.clear_tracks()
        for (tracknum,artist,sortartist,title,dur,origname,artistid,trkid) in trackdata:
                t = toc.Track(tracknum)
                t.filename = origname
                disc.tracks.append(t)

        return release

def get_musicbrainz_release(disc):
	""" Given a Disc object, try a bunch of methods to look up the release in
	musicbrainz.  If a releaseid is specified, use this, otherwise search by
	discid, then search by CD-TEXT and finally search by audio-fingerprinting.
	"""
	# If a release id has been specified, that takes precedence
	if disc.releaseid is not None:
		return lookups.get_release_by_releaseid(disc.releaseid)

	# Otherwise, lookup the releaseid using the discid as a key
        if disc.discid is not None:
                results = lookups.get_releases_by_discid(disc.discid)
                if len(results) > 1:
                        for result in results:
                                report(result.release.id + ".html")
                        report("Ambiguous DiscID, trying fingerprint matching")
                        return get_release_by_fingerprints(disc)

                # DiscID lookup gave us an exact match. Use this!
                if len(results) == 1:
                        releaseid = results[0].release.id
                        report("Got release via discID")
                        return lookups.get_release_by_releaseid(results[0].release.id)

	# Otherwise, use CD-TEXT if present to guess the release
	if disc.performer is not None and disc.title is not None:
		report("Trying to look up release via CD-TEXT")
		report("Performer: " + disc.performer)
		report("Title    : " + disc.title)
		results = lookups.get_releases_by_cdtext(performer=disc.performer, 
                                        title=disc.title, num_tracks=len(disc.tracks))
		if len(results) == 1:
			report("Got result via CD-TEXT lookup!")
			report("Suggest submitting TOC and discID to musicbrainz:")
			report("Release URL: " + results[0].release.id + ".html")
			report("Submit URL : " + submit.musicbrainz_submission_url(disc))
			return lookups.get_release_by_releaseid(results[0].release.id)
		elif len(results) > 1:
			for result in results:
				report(result.release.id + ".html")
			report("Ambiguous CD-TEXT")
		else:
			report("No results from CD-TEXT lookup.")

        # Last resort, fingerprinting
        report("Trying fingerprint search")
        return get_release_by_fingerprints(disc)

def scheme_help():
        print "Naming scheme help:"
        print "Naming schemes are specified as a standard Python string expansion. The default scheme is:"
        print albumidentifyconfig.config.get("renamealbum", "naming_scheme")
        print "A custom scheme can be specified with --scheme. The list of expandos are:"
        for i in string_expandos:
                print " " + i + " (string)"
        for i in integer_expandos:
                print " " + i + " (integer)"

def path_arg_cb(option, opt_str, value, parser):
        path = os.path.abspath(value)
        if not os.path.isdir(path):
		raise optparse.OptionValueError("to %s must be a directory that exists" % value)
	setattr(parser.values, option.dest, path)

def main():
	opts = optparse.OptionParser()
	opts.add_option(
		"-r","--release-id",
		dest="releaseid",
		default=None,
		metavar="MBRELEASEID",
		help="The Musicbrainz release id for this disc. Use this to specify the release when discid lookup fails.")
	opts.add_option(
		"--no-embed-coverart",
		dest="embedcovers",
		action="store_false",
		default=True,
		help="Don't embed the cover-art in each flac file.")
	opts.add_option(
		"--release-asin",
		dest="asin",
		metavar="ASIN",
		default=None,
		help="Manually specify the Amazon ASIN number for discs that have more than one ASIN (useful to force the correct coverart image)."
		)
	opts.add_option(
		"--year",
		dest="year",
		metavar="YEAR",
		default=None,
		help="Overwrite the album release year.  Use to force a re-issue to the date of the original release or to provide a date where one is missing"
		)
	opts.add_option(
		"-n","--no-act",
		dest="noact",
		action="store_true",
		default=False,
		help="Don't actually tag and rename files."
		)
	opts.add_option(
		"--total-discs",
		dest="totaldiscs",
		metavar="DISCS",
		default = None
		)
	opts.add_option(
		"--no-force-order",
		dest="force_order",
		action="store_false",
		default=True,
		help="Don't require source files to be in order. Note: May cause false positives."
		)
	opts.add_option(
		"--force-short-album",
		dest="force_short_album",
		action="store_true",
		default=False,
		help="We won't try and rename albums via fingerprinting if they are less than 3 tracks long. Use this to override."
		)
	opts.add_option(
		"--dest-path",
		dest="destprefix",
		type="str",
		action="callback",
		callback=path_arg_cb,
		default=False,
		metavar="PATH",
		help="Use PATH instead of the current path for creating output directories."
		)
	opts.add_option(
		"--scheme",
		dest="scheme",
		default= albumidentifyconfig.config.get("renamealbum", "naming_scheme"),
		metavar="SCHEME",
        	help="Specify a naming scheme, see --scheme-help"
		)
	opts.add_option(
		"--scheme-help",
		action="store_const",
		dest="action",
		const="scheme-help",
        	help="Help on naming schemes.",
		default="rename",
		)

	(options, args) = opts.parse_args()

	releaseid 	= options.releaseid
	embedcovers 	= options.embedcovers
	asin 		= options.asin
	year 		= options.year
	noact 		= options.noact
	totaldiscs 	= options.totaldiscs
        destprefix 	= options.destprefix
	scheme		= options.scheme
	force_short_album=options.force_short_album
	albumidentify.FORCE_ORDER = options.force_order
	
	if options.action=="scheme-help":
		scheme_help()
		sys.exit(1)
		
	if len(args) < 1:
		opts.print_help()
		sys.exit(1)

	srcpath = os.path.abspath(args[0])

	if not os.path.exists(srcpath):
		opts.print_help()
		sys.exit(2)
	
        try:
                check_scheme(scheme)
        except Exception, e:
                print "Naming scheme error: " + e.args[0]
                sys.exit(1)

        report("----renamealbum started----")

        print "Using naming scheme: " + scheme

	if noact:
		print "Performing dry-run"

	print "Source path: " + srcpath

	if os.path.exists(os.path.join(srcpath, "data.toc")):
                disc = toc.Disc(cdrdaotocfile = os.path.join(srcpath, "data.toc"))
	elif os.path.exists(os.path.join(srcpath, "TOC")):
                disc = toc.Disc(cdrecordtocfile = os.path.join(srcpath, "data.toc"))
        else:
                disc = toc.Disc()
                disc.dirname = srcpath

        if disc.tocfilename:
                disc.discid = discid.generate_musicbrainz_discid(
                                disc.get_first_track_num(),
                                disc.get_last_track_num(),
                                disc.get_track_offsets())
                report("Found TOC, calculated discID: " + disc.discid)

	if releaseid:
                report("Forcing releaseid: " + releaseid)
		disc.releaseid = releaseid
	
	release = get_musicbrainz_release(disc)

	if release is None:
                report("no releases found")
		raise Exception("Couldn't find a matching release. Sorry, I tried.")

        report("release id: %s.html" % release.id)

	disc.releasetypes = release.getTypes()

	disc.set_musicbrainz_tracks(release.getTracks())
	disc.releasedate = release.getEarliestReleaseDate()

	disc.artist = release.artist.name
	disc.album = release.title
	if year is not None:
		disc.year = year
		disc.releasedate = year
	elif disc.releasedate is not None:
		disc.year = disc.releasedate[0:4]
	else:
                report("couldn't determine year for %s - %s" % (`disc.artist`, `disc.album`))
		raise Exception("Unknown year: %s %s " % (`disc.artist`,`disc.album`))

	disc.compilation = 0
	disc.number = 0
	disc.totalnumber = 0
	if asin is not None:
		disc.asin = asin
	else:
		disc.asin = lookups.get_asin_from_release(release, prefer=".co.uk")
			
	# Set the compilation tag appropriately
	if musicbrainz2.model.Release.TYPE_COMPILATION in disc.releasetypes:
		disc.compilation = 1
	
	# Get album art
	imageurl = lookups.get_album_art_url_for_asin(disc.asin)
	# Check for manual image
        imagemime = None
        imagepath = None
        image_needs_unlink = False
	if os.path.exists(os.path.join(srcpath, "folder.jpg")):
		print "Using existing image"
		if not noact:
                        imagemime="image/jpeg"
                        imagepath = os.path.join(srcpath, "folder.jpg")
	elif imageurl is not None:
                print "Downloading album art from %s" % imageurl
		if not noact:
                        try:
                                (fd,tmpfile) = tempfile.mkstemp(suffix = ".jpg")
                                os.close(fd)
                                (f,h) = urllib.urlretrieve(imageurl, tmpfile)
                                if h.getmaintype() != "image":
                                        print "WARNING: image url returned unexpected mimetype: %s" % h.gettype()
                                        os.unlink(tmpfile)
                                else:
                                        imagemime = h.gettype()
                                        imagepath = tmpfile
                                        image_needs_unlink = True
                        except:
                                print "WARNING: Failed to retrieve coverart (%s)" % imageurl

	# Deal with disc x of y numbering
	(albumname, discnumber, disctitle) = lookups.parse_album_name(disc.album)
	if discnumber is None:
		disc.number = 1
		disc.totalnumber = 1
	elif totaldiscs is not None:
		disc.totalnumber = totaldiscs
		disc.number = int(discnumber)
	else:
		disc.number = int(discnumber)
		discs = lookups.get_all_releases_in_set(release.id)
		disc.totalnumber = len(discs)

	print "disc " + str(disc.number) + " of " + str(disc.totalnumber)

        disc.is_single_artist = release.isSingleArtistRelease()
	(srcfiles, destfiles, need_mp3_gain) = name_album(disc, release, srcpath, scheme, destprefix, imagemime, imagepath, embedcovers, noact)

        if (image_needs_unlink):
                os.unlink(imagepath)

        if (need_mp3_gain):
                os.spawnlp(os.P_WAIT, "mp3gain", "mp3gain",
                        "-a", # album gain
                        "-c", # ignore clipping warning
                        *destfiles)

supported_extensions = [".flac", ".ogg", ".mp3"]

def get_file_list(disc):
        # If the tracks don't have filenames attached, just use the files in
        # the directory as if they are already in order
        files = []
        if (disc.tracks[0].filename is None):
                files = [ x for x in os.listdir(disc.dirname) if x[x.rfind("."):] in supported_extensions ]
                files.sort()
        else:
                files = [ x.filename for x in disc.tracks ]
        return files

def check_scheme(scheme):
        """ Tries a dummy expansion on the naming scheme, raises an exception
            if the scheme contains expandos that we don't recognise.
        """
        dummyvalues = {}
        for k in string_expandos:
                dummyvalues[k] = "foo"
        for k in integer_expandos:
                dummyvalues[k] = 1
        try:
                scheme % dummyvalues
        except KeyError, e:
                raise Exception("Unknown expando in naming scheme: %s" % e.args)
        except ValueError, e:
                raise Exception("Failed to parse naming scheme: %s" % e.args)

def expand_scheme(scheme, disc, track, tracknumber):
        albumartist = mp3names.FixArtist(disc.artist)
	if musicbrainz2.model.Release.TYPE_SOUNDTRACK in disc.releasetypes:
                albumartist = "Soundtrack"

        trackartist = disc.artist
        if not disc.is_single_artist:
                trackartist = lookups.get_track_artist_for_track(track.mb_track)

        # We "fix" each component individually so that we can preserve forward
        # slashes in the naming scheme.
        expando_values = { "trackartist" : mp3names.FixFilename(trackartist),
                    "albumartist" : mp3names.FixFilename(disc.artist),
                    "sortalbumartist" : mp3names.FixFilename(mp3names.FixArtist(disc.artist)),
                    "sorttrackartist" : mp3names.FixFilename(mp3names.FixArtist(trackartist)),
                    "album" : mp3names.FixFilename(disc.album),
                    "year" : int(disc.year),
                    "tracknumber" : int(tracknumber),
                    "trackname" : mp3names.FixFilename(track.mb_track.title)
        }
        
        try:
                newpath = scheme % expando_values
        except KeyError, e:
                raise Exception("Unknown expando %s" % e.args)

        newpath = os.path.normpath(newpath)

        return newpath

def rmrf(dir):
        for root, dirs, files in os.walk(dir, topdown=False):
            for name in files:
                os.remove(os.path.join(root, name))
            for name in dirs:
                os.rmdir(os.path.join(root, name))
        os.rmdir(dir)

def calc_average_bitrate(filename):
        if filename.endswith(".mp3"):
                return calc_average_bitrate_mp3(parsemp3.parsemp3(filename))
        else:
                return 0

def calc_average_bitrate_mp3(parsed_data):
        return (reduce(lambda a,b:a+b,
                [ (rate*count) for (rate,count) in parsed_data["bitrates"].items() ])/
                        parsed_data["frames"])

def name_album(disc, release, srcpath, scheme, destprefix, imagemime=None, imagepath=None, embedcovers=False, noact=False, move=False):
        files = get_file_list(disc)

        if len(files) != len(disc.tracks):
                report("Number of files to rename (%i) != number of tracks in release (%i)" % (len(files), len(disc.tracks)))
                return

        tracknum = 0
        srcfiles = []
        destfiles = []
        need_mp3_gain = False

        # Step 1: Tag all of the files into a temporary directory
        tmpdir = tempfile.mkdtemp()
        tmpfiles = [] 

	for file in files:
                (root,ext) = os.path.splitext(file)
                tracknum = tracknum + 1
		track = disc.tracks[tracknum - 1]
		mbtrack = track.mb_track

                if mbtrack.title == "_silence_":
                        continue

                newpath = expand_scheme(scheme, disc, track, tracknum)
                newpath += ext

                if destprefix != "":
                        newpath = os.path.join(destprefix, newpath)
                else:
                        newpath = os.path.join(srcpath, "../%s" % newpath)

                newpath = os.path.normpath(newpath)
                newfilename = os.path.basename(newpath)

                print "Tagging: " + newfilename

                entry = {}
                entry["srcfilepath"] = os.path.join(srcpath, file)
                entry["tmpfilename"] = os.path.join(tmpdir, newfilename)
                entry["destfilepath"] = newpath
                tmpfiles.append(entry)

                srcfilepath = os.path.join(srcpath, file)

		if not noact and ext != ".mp3":
                        shutil.copyfile(os.path.join(srcpath, file), entry["tmpfilename"])

                track_artist = release.artist
                if not disc.is_single_artist:
                        track_artist = lookups.get_track_artist_for_track(track.mb_track)

                # Set up the tag list so that we can pass it off to the
                # container-specific tagger function later.
                tags = {}
                tags[tag.TITLE] = mbtrack.title
                tags[tag.ARTIST] = track_artist.name
                tags[tag.ALBUM_ARTIST] = disc.artist
                tags[tag.TRACK_NUMBER] = str(tracknum)
                tags[tag.TRACK_TOTAL] = str(len(disc.tracks))
                tags[tag.ALBUM] = disc.album
                tags[tag.ALBUM_ID] = os.path.basename(release.id)
                tags[tag.ALBUM_ARTIST_ID] = os.path.basename(release.artist.id)
                tags[tag.ARTIST_ID] = os.path.basename(track_artist.id)
                tags[tag.TRACK_ID] = os.path.basename(mbtrack.id)
                tags[tag.DATE] = disc.releasedate
                tags[tag.YEAR] = disc.year
                tags[tag.SORT_ARTIST] = mp3names.FixArtist(track_artist.name)
                tags[tag.SORT_ALBUM_ARTIST] = mp3names.FixArtist(disc.artist)

                if disc.discid:
                        tags[tag.DISC_ID] = disc.discid
                if disc.compilation:
                        tags[tag.COMPILATION] = "1"
                if track.isrc is not None:
                        tags[tag.ISRC] = track.isrc
                if disc.mcn is not None:
                        tags[tag.MCN] = disc.mcn
                for rtype in disc.releasetypes:
                        types = tags.get(tag.RELEASE_TYPES, [])
                        types.append(musicbrainz2.utils.getReleaseTypeName(rtype))
                        tags[tag.RELEASE_TYPES] = types
                if disc.totalnumber > 1:
                        tags[tag.DISC_NUMBER] = str(disc.number)
                        tags[tag.DISC_TOTAL_NUMBER] = str(disc.totalnumber)

                image = None
                if embedcovers and imagepath:
                        image = imagepath

                tag.tag(entry["tmpfilename"], tags, noact, image)

                # Special case mp3.. tag.tag() won't do anything with mp3 files
                # as we write out the tags + bitstream in one operation, so do
                # that here.
                if ((not noact) and (ext == ".mp3")):
                        # Make a temp copy and undo any mp3gain
                        (fd,tmpmp3) = tempfile.mkstemp(suffix=".mp3")
                        os.close(fd)
                        shutil.copy(srcfilepath, tmpmp3)
                        os.spawnlp(os.P_WAIT, "mp3gain", "mp3gain", "-u", "-q", tmpmp3)
                        parsed_data = parsemp3.parsemp3(tmpmp3)
                        outtags = tag.get_mp3_tags(tags)
                        outtags["bitstream"] = parsed_data["bitstream"]
                        if image:
                                imagefp=open(image, "rb")
                                imagedata=imagefp.read()
                                imagefp.close()
                                outtags["APIC"] = (imagemime,"\x03","",imagedata)
                        serialisemp3.output(entry["tmpfilename"], outtags)
                        need_mp3_gain = True
                        os.unlink(tmpmp3)

                srcfiles.append(srcfilepath)

        # Step 2: Compare old and new bitrates
        old_total_bitrate = 0
        new_total_bitrate = 0
        for entry in tmpfiles:
                if os.path.exists(entry["destfilepath"]):
                        old_total_bitrate += calc_average_bitrate(entry["destfilepath"])
                new_total_bitrate += calc_average_bitrate(entry["tmpfilename"])

        if old_total_bitrate == 0:
                report("Destination files do not exist, creating")
        elif old_total_bitrate == new_total_bitrate:
                report("Bitrates are the same, overwriting")
        elif old_total_bitrate < new_total_bitrate:
                report("Old bitrate lower than new bitrate (%d / %d)" % (old_total_bitrate, new_total_bitrate))
        elif old_total_bitrate > new_total_bitrate:
                report("Not overwriting, old bitrate higher than new (%d / %d)" % (old_total_bitrate, new_total_bitrate))
                rmrf(tmpdir)
                return (srcfiles, destfiles, False)

        # Step 3: Overwrite/create files if appropriate
        for entry in tmpfiles:
                newpath = entry["destfilepath"]
                newdir = os.path.dirname(newpath)
                newfile = os.path.basename(newpath)

                if not noact:
                        makedirs(newdir)
                        report(entry["srcfilepath"] + " -> " + newpath)
                        # Try renaming first, then fall back to copy/rm
                        try:
                                os.rename(entry["tmpfilename"], newpath)
                        except OSError:
                                shutil.copyfile(entry["tmpfilename"], newpath)
                                os.remove(entry["tmpfilename"])

                destfiles.append(newpath)

        # Move original TOC
        if disc.tocfilename:
                report(os.path.join(srcpath, disc.tocfilename) + " -> " +  os.path.join(newdir, os.path.basename(disc.tocfilename)))
                if not noact:
                        shutil.copyfile(os.path.join(srcpath, disc.tocfilename), os.path.join(newdir, os.path.basename(disc.tocfilename)))

        # Move coverart
        if imagepath and not noact:
                report(imagepath + " -> " + os.path.join(newdir, "folder.jpg"))
                shutil.copyfile(imagepath, os.path.join(newdir, "folder.jpg"))

	#os.system("rm \"%s\" -rf" % srcpath)

        rmrf(tmpdir)
        return (srcfiles, destfiles, need_mp3_gain)
	

if __name__ == "__main__":
        try:
                main()
	except SystemExit:
		raise
        except:
                (t,v,tb) = sys.exc_info()
                report(t)
                report(v)
                report(traceback.format_exception(t,v,tb))
                del tb
                report("fail!")
        else:
                report("success!")

	if srcpath is not None:
		write_report(os.path.join(sys.argv[1], "report.txt"))
	sys.exit(0)
