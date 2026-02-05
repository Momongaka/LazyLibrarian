[![Buy Me a Coffee](https://img.icons8.com/?size=24&id=gEtADbEGxPM2&format=png&color=000000) Buy Me a Coffee](https://buymeacoffee.com/philbormang)

## LazyLibrarian
LazyLibrarian is a program to follow authors and grab metadata for all your digital reading needs.
It uses a combination of [HardCover](https://hardcover.app) [OpenLibrary](https://openlibrary.org/) [Librarything](https://www.librarything.com/) [GoodReads](https://www.goodreads.com) and optionally [GoogleBooks](https://www.googleapis.com/books/v1/) as sources for author info and book info. License: GNU GPL v3

## IMPORTANT NOTE
LazyLibrarian used GoodReads extensively for author and book info, but they have now shut down their api. If you have an existing,working goodreads api key you can continue using it until they close completely, otherwise I suggest using HardCover and/or OpenLibrary.

## Description
Right now it's capable of the following:
* Import an existing calibre library (optional, you dont need calibre)
* Find authors and add them to the database
* List all books of an author and mark ebooks or audiobooks as 'wanted'.
* LazyLibrarian will search for a nzb-file or a torrent or magnet link for that book
* If a nzb/torrent/magnet is found it will be sent to a download client or saved in a black hole where your download client can pick it up.
* Currently supported download clients for usenet are :
- sabnzbd (versions later than 0.7.x preferred)
- nzbget
- synology_downloadstation
* Currently supported download clients for torrent and magnets are:
- deluge
- transmission
- utorrent
- qbittorrent
- rtorrent
- synology_downloadstation
* When processing the downloaded books it will save a cover picture (if available) and save all metadata into metadata.opf next to the bookfile (calibre compatible format)
* The new theme for the site allows it to be accessed from devices with a smaller screen (such as a tablet)
* AutoAdd feature for book management tools like Calibre which must have books in flattened directory structure, or use calibre to import your books into an existing calibre library
* LazyLibrarian can also be used to search for and download magazines, and monitor for new issues

## Install:
LazyLibrarian runs by default on port 5299 at http://localhost:5299

* Install Python 3 v3.7 or higher - later is better
* Git clone/extract LL wherever you like
* Install any missing dependencies using "pip install ."  or "python -m pip install ." (include the dot at the end)
* Run `python LazyLibrarian.py -d` or `python LazyLibrarian.py --daemon` to start in daemon mode
* Fill in all the config (see the docs)


## Documentation:
There is extensive documentation at https://lazylibrarian.gitlab.io/
and a reddit at https://www.reddit.com/r/LazyLibrarian/

Docker tutorial  http://sasquatters.com/lazylibrarian-docker/
Config tutorial  http://sasquatters.com/lazylibrarian-configuration/

## Update
Auto update available via interface from master for git and source installs

## Docker packages
LinuxServer : https://hub.docker.com/r/linuxserver/lazylibrarian/
The docker package includes ghostscript for magazine cover generation and calibredb (via optional variable)
LinuxServer docker is multi-arch and works on X86_64, armhf and aarch64 (calibredb only available on X86_64)
The dockers can be upgraded using the lazylibrarian internal upgrade mechanism

[![Buy Me a Coffee](https://img.icons8.com/?size=24&id=gEtADbEGxPM2&format=png&color=000000) Buy Me a Coffee](https://buymeacoffee.com/philbormang)
