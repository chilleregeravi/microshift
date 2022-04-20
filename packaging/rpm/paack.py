#!/bin/env python3
import argparse
import os
import sys
import tarfile
import yaml

SPEC_TEMPLATE = """# spec file generated by paack
Name: %%_NAME_%%

# disable dynamic rpmbuild checks
%global __os_install_post /bin/true
%global __arch_install_post /bin/true

AutoReqProv: no

%global imageStore %%_IMAGE_STORE_%%

Version: %%_VERSION_%%
Release: %%_RELEASE_%%

Summary: %%_SUMMARY_%%
License: %%_LICENSE_%%
%%_URL_%%

%%_SOURCE_FILES_%%

Requires: crio

%description

%%_DESCRIPTION_%%

%prep

if [ -d  %{buildroot}%{imageStore} ]
then
   rm -rf  %{buildroot}%{imageStore}
fi

%clean
find %{buildroot} -not -writable -not -type l -exec chmod u+w {} +
rm -rf  %{buildroot}

%install

mkdir -p %{buildroot}%{imageStore}
cd %{buildroot}%{imageStore}

%%_EXTRACT_ARCHS_%%

# fix permissions here so that cpio can work later
find ./ -perm 000 -exec chmod 400 {} +

%post

set -x 

# only on install (1), not on upgrades (2)
if [ $1 -eq 1 ]; then
   sed -i '/^additionalimagestores =*/a "%{imageStore}",' /etc/containers/storage.conf
   # if crio was already started, restart it so it read from new imagestore
   systemctl is-active --quiet crio && systemctl restart --quiet crio || true
fi

%postun

# only on uninstall (0), not on upgrades(1)
if [ $1 -eq 0 ]; then
  sed -i '\:%{imageStore}:d' /etc/containers/storage.con
  systemctl is-active --quiet crio && systemctl restart --quiet crio || true
fi

%files
%%_FILES_%%

%changelog
* Fri Apr 1 2022 Paack <paack@redhat-et-edge> . %%_VERSION_%%-%%_RELEASE_%%
Read only crio containter storage generated by paack
"""

BLUE="\u001b[1;34m"
YELLOW="\u001b[1;33m"
RED="\u001b[1;31m"
GREEN="\u001b[32m"
CLEAR="\u001b[0m"


class SpecFile(object):
    def __init__(self, name, filename):
        self._spec = SPEC_TEMPLATE
        self._filename = filename
        self._files_data = "" # content for the %files section
        self._extract_archs = ""
        self._name = name
        self._source_i =0
        self._sources = ""
        self._license = None
        self._version = None
        self._release = None
        self._summary = None
        self._image_store = None
        self._url = None

    def _set(self, field, contents, required, default=''):
        if contents is None and required:
            print("field %s is missing" % field)
            sys.exit(1)

        self._spec = self._spec.replace('%%_'+field+'_%%', str(contents or default))

    def set_version(self, version):
        self._version = version

    def set_release(self, release):
        self._release = release

    def set_license(self, license):
        self._license = license

    def set_summary(self, summary):
        self._summary = summary

    def set_description(self, description):
        self._description = description

    def set_image_store(self, image_store):
        if not image_store.endswith('/'):
            image_store += '/'
        self._image_store = image_store

    def set_url(self, url):
        self._url = url

    def write(self):
        self._set('NAME', self._name, True)
        self._set('FILES', self._files_data, True)
        self._set('VERSION', self._version, True)
        self._set('RELEASE', self._release, False, '0')
        self._set('LICENSE', self._license, False, 'Unknown')
        self._set('SUMMARY', self._summary, False)
        self._set('DESCRIPTION', self._description, False)
        self._set('IMAGE_STORE', self._image_store, False, '/usr/lib/container-images/' + self._name + '/')

        if self._url:
            self._set('URL', "Url: " + self._url, False)
        else:
            self._set('URL', "", False)

        self._set('SOURCE_FILES', self._sources, True)
        self._set('EXTRACT_ARCHS', self._extract_archs, True)

        with open(self._filename, "w") as f_out:
            f_out.write(self._spec)

        return self._filename

    @staticmethod
    def _get_files(tar_filename):
        with tarfile.open(tar_filename) as tar:
            for tarinfo in tar:
                yield tarinfo

    def scan_files(self, tar, arch):
        output = ""

        for info in SpecFile._get_files(tar):
            if info.type in [tarfile.LNKTYPE, tarfile.REGTYPE]:
                # directories, hard links and regular files
                output += "%%attr(%o,%d,%d) \"%%{imageStore}%s\"\n" % (info.mode, info.uid, info.gid, info.name)
            if info.type in [tarfile.SYMTYPE]:
                # symlinks have no mode permissions
                output += "%%attr(-,%d,%d) \"%%{imageStore}%s\"\n" % (info.uid, info.gid, info.name)
            if info.type in [tarfile.DIRTYPE]:
                # directories, hard links and regular files
                output += "%%dir %%attr(%o,%d,%d) \"%%{imageStore}%s\"\n" % (info.mode, info.uid, info.gid, info.name)

        self._files_data += "\n\n%ifarch " + arch + "\n" + output + "%endif\n"
        self._extract_archs += "\n\n%ifarch " + arch + "\n" + "tar xfj %{SOURCE"+str(self._source_i) + "}\n%endif"
        self._sources += "Source%d: %s\n" % (self._source_i, os.path.basename(tar))
        self._source_i += 1


class PaackYaml(object):
    def __init__(self, filename):
        with open(filename, "r") as f:
            try:
                self._data = yaml.safe_load(f)
            except yaml.YAMLError as exc:
                print(exc)
                sys.exit(1)
        self.check()

    def data(self):
        return self._data # TODO: instead of this, provide methods to traverse the content, this interface is too loose

    def check(self):
        pass # TODO


def system(cmd):
    print(YELLOW + "> " + cmd + CLEAR)
    result = os.system(cmd)
    if result != 0:
        print(RED + "command failed" + CLEAR)
        sys.exit(result)
    return result



class SRPMBuilderCommand(object):
    def __init__(self, args):
        self._rpmbuild_dir = args.rpmbuild_dir.replace("~", os.environ.get("HOME"))
        self._tmp = args.tmp
        self._yaml = args.yaml
        self._no_cleanup = args.no_cleanup

    def build(self):
        return self._build_from_yaml()

    def _build_from_yaml(self):
        data = PaackYaml(self._yaml).data()

        # ensure output directory
        system("mkdir -p " + os.path.join(self._rpmbuild_dir, 'SPECS'))

        result_files = []

        for package in data['packages']:
            out_srpm = os.path.join(self._rpmbuild_dir, 'SRPMS',
                                    '%s-%s-%s.src.rpm' % (package['name'], package['version'], package['release']))

            # if we are in no-cleanup mode the existing srpm is reused instead
            if not (os.path.exists(out_srpm) and self._no_cleanup):
                spec = SpecFile(package['name'], os.path.join(self._rpmbuild_dir, 'SPECS',
                    '%s-%s-%s.spec' % (package['name'], package['version'], package['release'])))
                for arch_info in package['arch']:
                    arch = arch_info['name']
                    tar_path = self._create_tarball(package, arch_info)
                    print("scanning %s to generate files section for arch %s" % (tar_path, arch))
                    spec.scan_files(tar_path, arch)

                spec.set_version(package['version'])
                spec.set_release(package.get('release'))
                spec.set_license(package.get('license'))
                spec.set_summary(package.get('summary'))
                spec.set_description(package.get('description'))
                spec.set_image_store(package.get('path'))
                spec.set_url(package.get('url'))

                spec_filename = spec.write()
                print("%s written." % spec_filename)

                print("building srpm for " + spec_filename)
                system('rpmbuild -bs --define "_topdir %s" %s' % (self._rpmbuild_dir, spec_filename))

            result_files.append(out_srpm)

        return result_files

    def _create_tarball(self, package, arch_info):
        tar_dir = os.path.join(self._rpmbuild_dir, 'SOURCES')
        tar_file = '%s-%s-%s-%s.tar.bz2' % (package['name'], package['version'], package['release'], arch_info['name'])
        tar_path = os.path.join(tar_dir, tar_file)

        if os.path.exists(tar_path) and self._no_cleanup:
            print("already exists %s" % tar_path)
        else:
            directory = os.path.join(self._tmp, package['name'], arch_info['name'])
            system("mkdir -p " + directory)
            for image in arch_info['images']:
                arch_name = arch_info.get('image_arch', arch_info['name'])
                print("pulling %s for arch %s, to %s" % (image, arch_name, directory))
                result = system('sudo podman pull --arch %s --root "%s" %s' % (arch_name, directory, image))
                if result!=0:
                    print("error pulling image")
                    sys.exit(result)

            system("mkdir -p " + tar_dir)

            print("creating %s" % tar_path)
                    # root permissions needed to capture all files with various uid/gid/perms
            system("cd %s; sudo tar cfj %s . && sudo chmod a+rw %s" % (directory, tar_path, tar_path))

        return tar_path


parser = argparse.ArgumentParser()
parsers = parser.add_subparsers(dest="command")


srpm_cmd = parsers.add_parser("srpm", help="Build srpm from yaml definition")
srpm_cmd.add_argument('yaml', type=str, help='yaml definition')
srpm_cmd.add_argument('-t', '--tmp', type=str, help='tmp directory', default='/tmp/containers')
srpm_cmd.add_argument('-r', '--rpmbuild_dir', type=str, default="~/rpmbuild")
srpm_cmd.add_argument('-n', '--no-cleanup', action='store_true', help="Don't cleanup the tmp container directories")


copr_cmd = parsers.add_parser("copr", help="Build from yaml definition")
copr_cmd.add_argument('yaml', type=str, help='yaml definition')
copr_cmd.add_argument('copr_repo', type=str, help='copr repository, like @redhat-et/microshift-containers')
copr_cmd.add_argument('-t', '--tmp', type=str, help='tmp directory', default='/tmp/containers')
copr_cmd.add_argument('-r', '--rpmbuild_dir', type=str, default="~/rpmbuild")
copr_cmd.add_argument('-n', '--no-cleanup', action='store_true', help="Don't cleanup the tmp container directories")
copr_cmd.add_argument('-N', '--no-wait', action='store_true', help="Don't wait for copr builds to finish")

rpm_cmd = parsers.add_parser("rpm", help="Build via mock from yaml definition")
rpm_cmd.add_argument('yaml', type=str, help='yaml definition')
rpm_cmd.add_argument('target', type=str, help='mock target, like centos-stream-9-aarch64, see /etc/mock/*.cfg' )
rpm_cmd.add_argument('-t', '--tmp', type=str, help='tmp directory', default='/tmp/containers')
rpm_cmd.add_argument('-r', '--rpmbuild_dir', type=str, default="~/rpmbuild")
rpm_cmd.add_argument('-n', '--no-cleanup', action='store_true', help="Don't cleanup the tmp container directories")

args = parser.parse_args()

def _report_generated_files(Gfiles):
    print(GREEN + "The following srpm files have been generated:" + CLEAR)
    for f in files:
        print(" * " + BLUE + f + CLEAR)

if args.command == "srpm":
    builder = SRPMBuilderCommand(args)
    files = builder.build()
    _report_generated_files(files)

elif args.command == "copr":
    builder = SRPMBuilderCommand(args)
    files = builder.build()
    _report_generated_files(files)
    for f in files:
        extra_args = ""
        if args.no_wait:
            extra_args += "--no-wait "
        system("copr build " + extra_args + args.copr_repo + " " + f)

elif args.command == "rpm":
    builder = SRPMBuilderCommand(args)
    files = builder.build()
    _report_generated_files(files)
    print("")
    print(GREEN + "Building via mock for the target platform: %s" % args.target + CLEAR)
    for f in files:
        system("mock -r %s %s" % (args.target, f))

    print(GREEN + "your output files can be found in /var/lib/mock/" + args.target + "/" + CLEAR)