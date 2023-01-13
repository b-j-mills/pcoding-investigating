import logging
import re
from fiona import listlayers
from geopandas import read_file
from glob import glob
from hxl.geo import LAT_PATTERNS, LON_PATTERNS
from os import mkdir
from os.path import basename, dirname, join
from pandas import concat, isna, read_csv, read_excel
from shutil import rmtree
from zipfile import ZipFile, is_zipfile

from hdx.location.country import Country
from hdx.utilities.uuid import get_uuid

logger = logging.getLogger(__name__)


def download_resource(resource, fileext, resource_folder):
    try:
        _, resource_file = resource.download(folder=resource_folder)
    except:
        error = f"Could not download file {resource['name']}"
        return None, error

    if is_zipfile(resource_file) or ".zip" in basename(resource_file):
        temp = join(resource_folder, get_uuid())
        try:
            with ZipFile(resource_file, "r") as z:
                z.extractall(temp)
        except:
            error = f"Could not unzip resource {resource['name']}"
            return None, error
        resource_files = glob(join(temp, "**", f"*.{fileext}"), recursive=True)
        if len(resource_files) > 1:  # make sure to remove directories containing the actual files
            resource_files = [r for r in resource_files
                              if sum([r in rs for rs in resource_files if not rs == r]) == 0]
        if fileext == "xlsx" and len(resource_files) == 0:
            resource_files = [resource_file]
        if fileext in ["gdb", "gpkg"]:
            resource_files = [join(r, i) for r in resource_files for i in listlayers(r)]
    else:
        resource_files = [resource_file]

    return resource_files, None


def read_downloaded_data(resource_files, fileext):
    data = dict()
    error = None
    for resource_file in resource_files:
        if fileext in ["xlsx", "xls"]:
            try:
                contents = read_excel(
                    resource_file, sheet_name=None, nrows=100
                )
            except:
                error = f"Unable to read resource {basename(resource_file)}"
                continue
            for key in contents:
                if contents[key].empty:
                    continue
                data[get_uuid()] = parse_tabular(contents[key], fileext)
        if fileext == "csv":
            try:
                contents = read_csv(resource_file, nrows=100, skip_blank_lines=True)
                data[get_uuid()] = parse_tabular(contents, fileext)
            except:
                error = f"Unable to read resource {basename(resource_file)}"
                continue
        if fileext in ["geojson", "json", "shp", "topojson"]:
            try:
                data = {
                    get_uuid(): read_file(resource_file, rows=100)
                }
            except:
                error = f"Unable to read resource {basename(resource_file)}"
                continue
        if fileext in ["gdb", "gpkg"]:
            try:
                data = {
                    get_uuid(): read_file(dirname(resource_file), layer=basename(resource_file), rows=100)
                }
            except:
                error = f"Unable to read resource {basename(resource_file)}"
                continue

    return data, error


def parse_tabular(df, fileext):
    df = df.dropna(how="all", axis=0).dropna(how="all", axis=1).reset_index(drop=True)
    df.columns = [str(c) for c in df.columns]
    if all([bool(re.match("Unnamed.*", c)) for c in df.columns]):  # if all columns are unnamed, move down a row
        df.columns = [str(c) if not isna(c) else f"Unnamed: {i}" for i, c in enumerate(df.loc[0])]
        df = df.drop(index=0).reset_index(drop=True)
    if not all(df.dtypes == "object"):  # if there are mixed types, probably read correctly
        return df
    if len(df) == 1:  # if there is only one row, return
        return df
    hxlrow = None  # find hxl row and incorporate into header
    i = 0
    while i < 10 and i < len(df) and not hxlrow:
        hxltags = [bool(re.match("#.*", t)) if t else True for t in df.loc[i].astype(str)]
        if all(hxltags):
            hxlrow = i
        i += 1
    if hxlrow:
        columns = []
        for c in df.columns:
            cols = [col for col in df[c][:hxlrow + 1] if col]
            if "Unnamed" not in c:
                cols = [c] + cols
            columns.append("||".join(cols))
        df.columns = columns
        df = df.drop(index=range(hxlrow + 1)).reset_index(drop=True)
        return df
    if fileext == "csv" and not hxlrow:  # assume first row of csv is header if there are no hxl tags
        return df
    columns = []
    datarow = 3
    if hxlrow:
        datarow = hxlrow + 1
    if len(df) < 3:
        datarow = len(df)
    for c in df.columns:
        cols = [str(col) for col in df[c][:datarow] if col]
        if "Unnamed" not in c:
            cols = [c] + cols
        columns.append("||".join(cols))
    df.columns = columns
    df = df.drop(index=range(datarow)).reset_index(drop=True)
    return df


def check_pcoded(contents):
    pcoded = None
    c = Country.countriesdata().get("countries", {})
    iso3s = [c[iso]["#country+code+v_iso3"] for iso in c]
    iso2s = [c[iso]["#country+code+v_iso2"] for iso in c]
    pcode_exp = "(" + "|".join(iso3s+iso2s) + ")" + "\d{1,}"
    header_exp = "((adm)?.*p?.?cod.*)|(#\s?adm\s?\d?\+?\s?p?(code)?)"
    for key in contents:
        if pcoded:
            break
        content = contents[key]
        content = content.select_dtypes(include=["string", "object"])
        for h in content.columns:
            if pcoded:
                break
            headers = h.split("||")
            pcoded_header = any([bool(re.match(header_exp, head, re.IGNORECASE)) for head in headers])
            if not pcoded_header:
                continue
            column = content[h].dropna()
            matches = sum(column.str.match(pcode_exp, case=False))
            if (len(column) - matches) <= 5 and matches > 0:
                pcoded = True

    return pcoded


def check_latlong(contents):
    latlonged = None
    lat_header_exp = "(.*latitude?.*)|(lat)|((point.?)?y)|(#\s?geo\s?\+\s?lat)"
    lon_header_exp = "(.*longitude?.*)|(lon(g)?)|((point.?)?x)|(#\s?geo\s?\+\s?lon)"
    for key in contents:
        if latlonged:
            break
        content = contents[key]
        latted = None
        longed = None
        for h in content.columns:
            if latlonged:
                break
            headers = h.split("||")
            lat_header = any([bool(re.match(lat_header_exp, head, re.IGNORECASE)) for head in headers])
            lon_header = any([bool(re.match(lon_header_exp, head, re.IGNORECASE)) for head in headers])
            if not lat_header and not lon_header:
                continue
            column = content[h].dropna().astype(str)
            if lat_header:
                matches = concat([column.str.match(lat_exp.pattern, case=False) for lat_exp in LAT_PATTERNS], axis=1)
                matches = sum(matches.any(axis=1))
                if (len(column) - matches) <= 5 and matches > 0:
                    latted = True
            if lon_header:
                matches = concat([column.str.match(lon_exp.pattern, case=False) for lon_exp in LON_PATTERNS], axis=1)
                matches = sum(matches.any(axis=1))
                if (len(column) - matches) <= 5 and matches > 0:
                    longed = True

            if latted and longed:
                latlonged = True

    return latlonged


def check_location(dataset, temp_folder):
    pcoded = None
    latlonged = None
    error = None

    allowed_filetypes = ["csv", "geodatabase", "geojson", "geopackage", "json",
                         "shp", "topojson", "xls", "xlsx"]
    filetypes = dataset.get_filetypes()
    if not any(f in allowed_filetypes for f in filetypes):
        error = "Can't check formats"
        return None, None, error

    resource_folder = join(temp_folder, get_uuid())
    mkdir(resource_folder)

    resources = dataset.get_resources()
    for resource in resources:
        if pcoded:
            break

        filetype = resource.get_file_type()
        fileext = filetype
        if fileext == "geodatabase":
            fileext = "gdb"
        if fileext == "geopackage":
            fileext = "gpkg"

        if filetype not in allowed_filetypes:
            continue

        logger.info(f"Checking {resource['name']}")

        resource_files, error = download_resource(resource, fileext, resource_folder)
        if not resource_files:
            return pcoded, latlonged, error

        contents, error = read_downloaded_data(resource_files, fileext)

        if not pcoded:
            pcoded = check_pcoded(contents)
        if not pcoded and not latlonged and filetype in ["csv", "json", "xls", "xlsx"]:
            latlonged = check_latlong(contents)

    if not error and not pcoded:
        pcoded = False
    if not error and not pcoded and not latlonged:
        latlonged = False

    rmtree(resource_folder)

    return pcoded, latlonged, error
