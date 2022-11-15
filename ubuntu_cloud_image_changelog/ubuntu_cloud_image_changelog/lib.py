"""Main module."""
import os
import logging
import re
import urllib.parse

import click
from debian.changelog import Changelog
from debian.debian_support import Version
from lazr.restfulclient.errors import NotFound


def get_source_package_details(
    ubuntu, launchpad, lp_arch_series, binary_package_name, binary_package_version, ppas
):
    # find the published binary for this series, binary_package_name
    # and binary_package_version
    source_package_name = None
    source_package_version = None
    archive = ubuntu.main_archive
    binaries = archive.getPublishedBinaries(
        exact_match=True,
        binary_name=binary_package_name,
        distro_arch_series=lp_arch_series,
        order_by_date=True,
        version=binary_package_version,
    )
    if len(binaries):
        # now get the source package name so we can get the changelog
        source_package_name = binaries[0].source_package_name
        source_package_version = binaries[0].source_package_version
    else:
        # search through the PPAs to see if this binary version was published
        # there.
        for ppa in ppas:
            ppa_owner, ppa_name = ppa.split("/")
            archive = launchpad.people[ppa_owner].getPPAByName(name=ppa_name)
            # using pocket "Release" when using a PPA ...'
            pocket = "Release"
            binaries = archive.getPublishedBinaries(
                exact_match=True,
                binary_name=binary_package_name,
                distro_arch_series=lp_arch_series,
                pocket=pocket,
                order_by_date=True,
                version=binary_package_version,
            )
            if len(binaries):
                # now get the source package name so we can get the changelog
                source_package_name = binaries[0].source_package_name
                source_package_version = binaries[0].source_package_version
    if not source_package_name or not source_package_version:
        raise click.ClickException(
            "Unable to find source package for {} {}".format(
                binary_package_name, binary_package_version
            )
        )
    return source_package_name, source_package_version


def arch_independent_package_name(package_name):
    # packages ending with ':amd64' or ':arm64' are special
    if package_name.endswith(":amd64") or package_name.endswith(":arm64"):
        package_name = package_name[:-6]
    return package_name


def _parse_cve_details(changelog_block, launchpad):
    changelog_block_cves = []
    for change in changelog_block:
        if "CVE" in change:
            cve_pos = [
                (m.start(), m.end()) for m in re.finditer(r"CVE-\d+-\d+", change)
            ]
            for start, end in cve_pos:
                cve = change[start:end].strip()
                if cve not in [
                    changelog_block_cve["cve"]
                    for changelog_block_cve in changelog_block_cves
                ]:
                    cve_details = {}
                    cve_details["cve"] = cve
                    cve_details["url"] = _get_cve_url(cve)
                    cve_details_lines = _get_cve_details(cve, launchpad)
                    cve_ubuntu_description = ""
                    cve_priority = "n/a"
                    cve_description = ""
                    cve_public_date = ""
                    for cve_details_line in cve_details_lines:
                        # only get the CVE description if the user has requested it
                        if not cve_ubuntu_description and cve_details_line.startswith(
                            "Ubuntu-Description:"
                        ):
                            # get the string in the line after the Ubuntu-Description: line
                            # while the next line is not 'Notes' keep appending to cve_description
                            while True:
                                next_line = next(cve_details_lines)
                                if next_line.startswith("Notes"):
                                    break
                                cve_ubuntu_description += next_line
                        if not cve_description and cve_details_line.startswith(
                            "Description:"
                        ):
                            # get the string in the line after the Description: line
                            # while the next line is not 'Notes' keep appending to cve_description
                            while True:
                                next_line = next(cve_details_lines)
                                if next_line.startswith("Ubuntu-Description:"):
                                    break
                                cve_description += next_line
                        if "Priority:" in cve_details_line:
                            cve_priority = cve_details_line.split("Priority:")[
                                1
                            ].strip()
                        if "PublicDate:" in cve_details_line:
                            cve_public_date = cve_details_line.split("PublicDate:")[
                                1
                            ].strip()

                    cve_details["cve_description"] = (
                        cve_ubuntu_description.lstrip()
                        if cve_ubuntu_description
                        else cve_description.lstrip()
                    )
                    cve_details["cve_priority"] = cve_priority
                    cve_details["cve_public_date"] = cve_public_date
                    changelog_block_cves.append(cve_details)
    return changelog_block_cves


def _get_cve_url(cve_number):
    """returns a url to CVE data from a cve number"""
    url = "https://ubuntu.com/security"
    return "{}/{}".format(url, cve_number)


def _get_cve_details(cve, launchpad):
    # download the cve details and parse so we can get the CVE description and the CVE priority
    cve_details_lines = []
    possible_cve_detail_locations = ["active", "retired", "ignored"]
    for possible_cve_detail_location in possible_cve_detail_locations:
        try:
            cve_details_url = (
                "https://git.launchpad.net/ubuntu-cve-tracker/plain/{}/{}".format(
                    possible_cve_detail_location, cve
                )
            )
            cve_details_resp = launchpad._browser.get(cve_details_url).decode("utf-8")
            cve_details_lines = iter(cve_details_resp.splitlines())
            return cve_details_lines
        except NotFound:
            pass  # Keep trying until we find the cve details
    return cve_details_lines


def parse_changelog(
    launchpad,
    changelog_filename,
    from_version=None,
    to_version=None,
    count=1,
    highlight_cves=False,
):
    """
    Extract changelog entries within a version range

    The range of changelog entries returned will include all entries
    after version_low up to, and including, version_high.
    If either the starting or ending version are not found in the
    list of changelog entries the result will be incomplete and
    a non-empty error message is returned to indicate the issue.
    """
    changelog = ""
    changelogs = []
    if not to_version:
        raise Exception("to_version must be specified when parsing changelog")

    with open(changelog_filename, "r") as fileptr:
        try:
            parsed_changelog = Changelog(fileptr.read())
            changelog += "Source: {}\n".format(parsed_changelog.get_package())
            changelog += "Version: {}\n".format(parsed_changelog.version)
            changelog += "Distribution: {}\n".format(parsed_changelog.distributions)
            changelog += "Urgency: {}\n".format(parsed_changelog.urgency)
            changelog += "Maintainer: {}\n".format(parsed_changelog.author)
            changelog += "Date: {}\n".format(parsed_changelog.date)
            # The changelog blocks are in reverse order; we'll see high|to before low|from.
            change_blocks = []
            launchpad_bugs_fixed = []
            cves_referenced = []
            for changelog_block in parsed_changelog:
                if changelog_block.version:
                    # if changelog_block.version is None then this is a
                    # changelog we are unable to parse
                    changelog_version_equal_to_or_before_to_version = Version(
                        changelog_block.version.full_version
                    ) <= Version(to_version)

                    if from_version:
                        if Version(changelog_block.version.full_version) <= Version(
                            from_version
                        ):
                            # If we have reached our from version then we can
                            # stop parsing
                            break

                    if changelog_version_equal_to_or_before_to_version:
                        changelog_dict = {}
                        # Attempt to parse theCVEs referenced in the changelo entries
                        if highlight_cves:
                            cves = _parse_cve_details(
                                changelog_block.changes(), launchpad
                            )
                            cves_referenced += cves
                            changelog_dict["cves"] = cves
                        launchpad_bugs_fixed += changelog_block.lp_bugs_closed
                        changeblock_summary = "{} ({}) {}; urgency={}".format(
                            changelog_block.package,
                            changelog_block.version,
                            changelog_block.distributions,
                            changelog_block.urgency,
                        )
                        change_blocks.append((changeblock_summary, changelog_block))
                        changelog_dict["log"] = []
                        for change in changelog_block.changes():
                            changelog_dict["log"].append(change)
                        changelog_dict["package"] = changelog_block.package
                        changelog_dict["version"] = str(changelog_block.version)
                        changelog_dict["urgency"] = changelog_block.urgency
                        changelog_dict["distributions"] = changelog_block.distributions
                        changelog_dict[
                            "launchpad_bugs_fixed"
                        ] = changelog_block.lp_bugs_closed
                        changelog_dict["author"] = changelog_block.author
                        changelog_dict["date"] = changelog_block.date

                        changelogs.append(changelog_dict)
                    if count and len(change_blocks) == count:
                        break  # we have enough blocks now

            changelog += "Launchpad-Bugs-Fixed: {}\n".format(launchpad_bugs_fixed)
            changelog += "Changes:\n"
            for changeblock_summary, change_block in change_blocks:
                changelog += "{}\n".format(changeblock_summary)
                for change in change_block.changes():
                    changelog += "{}\n".format(change)
            # log a warning if we have no changelog or
            # from_version  ot found or to_version not found
            if not changelog:
                logging.warning(
                    "Unable to parse changelog {} for versions {} to {}".format(
                        changelog_filename, from_version, to_version
                    )
                )
        except Exception as ex:
            raise ex
            raise Exception(
                "Unable to parse package changelog {}".format(changelog_filename)
            )

        return changelog, cves_referenced, changelogs


def get_changelog(
    launchpad,
    ubuntu,
    lp_series,
    cache_directory,
    source_package_name,
    source_package_version,
    ppas,
):
    """
    Download changelog for source / version and returns path to that
    :param launchpad: launchpad
    :param ubuntu: ubuntu
    :param lp_series: lp_series
    :param str source_package_name: Binary package name
    :param str source_package_version: Package version
    :param list ppas: List of possible ppas package installed from
    :raises Exception: If changelog file could not be downloaded
    :return: changelog file for source package & version
    :param str image_architecture: Architecture of the image which the manifest belongs to
    :rtype: str
    """

    cache_filename = "%s/changelog.%s_%s" % (
        cache_directory,
        source_package_name,
        source_package_version,
    )

    if os.path.isfile(cache_filename):
        logging.debug(
            "Using cached changelog for %s:%s",
            source_package_name,
            source_package_version,
        )
        return cache_filename

    package_version_in_archive_changelog = False
    package_version_in_ppa_changelog = False
    with open(cache_filename, "wb") as cache_file:
        archive = ubuntu.main_archive

        # Get the published sources for this exact version
        sources = archive.getPublishedSources(
            exact_match=True,
            source_name=source_package_name,
            distro_series=lp_series,
            order_by_date=True,
            version=source_package_version,
        )
        if len(sources):
            archive_changelog_url = sources[0].changelogUrl()

            _patched_archive_changelog_url = launchpad._root_uri.append(
                urllib.parse.urlparse(archive_changelog_url).path.lstrip("/")
            )

            archive_changelog = launchpad._browser.get(_patched_archive_changelog_url)

            if source_package_version in archive_changelog.decode("utf-8"):
                cache_file.write(archive_changelog)
                package_version_in_archive_changelog = True

        if not package_version_in_archive_changelog:
            # Attempt to get the changelog from any of the passed in PPAs instead
            for ppa in ppas:
                ppa_owner, ppa_name = ppa.split("/")
                archive = launchpad.people[ppa_owner].getPPAByName(name=ppa_name)
                # using pocket "Release" when using a PPA ...'
                pocket = "Release"
                sources = archive.getPublishedSources(
                    exact_match=True,
                    pocket=pocket,
                    source_name=source_package_name,
                    distro_series=lp_series,
                    order_by_date=True,
                    version=source_package_version,
                )
                if len(sources):
                    ppa_changelog_url = sources[0].changelogUrl()

                    _patched_ppa_changelog_url = launchpad._root_uri.append(
                        urllib.parse.urlparse(ppa_changelog_url).path.lstrip("/")
                    )

                    ppa_changelog = launchpad._browser.get(_patched_ppa_changelog_url)

                    if source_package_version in ppa_changelog.decode("utf-8"):
                        cache_file.write(ppa_changelog)
                        package_version_in_ppa_changelog = True
                        break  # no need to continue iterating the PPA list

        if (
            not package_version_in_archive_changelog
            and not package_version_in_ppa_changelog
        ):
            # can be found for this package and package version
            cache_file.write(
                "Unable to find changelog for srouce package {} "
                "version {}.".format(
                    source_package_name, source_package_version
                ).encode("utf-8")
            )

    return cache_filename
