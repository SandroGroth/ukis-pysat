#!/usr/bin/env python3

import json
import logging
import os
import shutil
import traceback

import fiona
import landsatxplore.api
import numpy as np
import pyproj
import requests
import sentinelsat
from io import BytesIO
from PIL import Image
from pylandsat import Product
from shapely import geometry, wkt, ops

from ukis_pysat.members import Datahub, Platform
from ukis_pysat.file import env_get, pack


logger = logging.getLogger(__name__)


class Source:
    """
    This class provides methods to query data sources for metadata and download images and quicklooks (APIs only).
    Remote APIs and local data directories that hold metadata files are supported.
    """

    def __init__(self, source, source_dir=None):
        """
        :param source: Name of the data source ['file', 'scihub', 'earthexplorer'] (String).
        :param source_dir: Path to directory if source is 'file' (String).
        """
        self.src = source

        if self.src == Datahub.file:
            if not source_dir:
                raise Exception(f"{traceback.format_exc()} source_dir has to be set if source is {self.src}.")
            else:
                self.api = source_dir

        elif self.src == Datahub.EarthExplorer:
            try:
                self.user = env_get("EARTHEXPLORER_USER")
                self.pw = env_get("EARTHEXPLORER_PW")
                self.api = landsatxplore.api.API(self.user, self.pw)
            except Exception as e:
                raise Exception(
                    f"{traceback.format_exc()} Could not connect to EarthExplorer. This Exception was raised {e}."
                )

        elif self.src == Datahub.Scihub:
            try:
                self.user = env_get("SCIHUB_USER")
                self.pw = env_get("SCIHUB_PW")
                self.api = sentinelsat.SentinelAPI(
                    self.user, self.pw, "https://scihub.copernicus.eu/dhus", show_progressbars=False,
                )
            except Exception as e:
                raise Exception(
                    f"{traceback.format_exc()} Could not connect to SciHub. This Exception was raised: {e}."
                )

        else:
            raise NotImplementedError(f"{source} is not supported [file, earthexplorer, scihub]")

    def __enter__(self):
        return self

    @staticmethod
    def _prep_aoi(aoi):
        """ This method converts aoi to Shapely Polygon and reprojects to WGS84.

        :param aoi: Area of interest as Geojson file or bounding box in lat lon coordinates (String, Tuple)
        :return: Shapely Polygon
        """
        if isinstance(aoi, str):
            with fiona.open(aoi, "r") as aoi:
                # make sure crs is in epsg:4326
                project = pyproj.Transformer.from_proj(
                    proj_from=pyproj.Proj(aoi.crs["init"]),
                    proj_to=pyproj.Proj("epsg:4326"),
                    skip_equivalent=True,
                    always_xy=True,
                )
                aoi = ops.transform(project.transform, geometry.shape(aoi[0]["geometry"]))

        elif isinstance(aoi, tuple):
            aoi = geometry.box(aoi[0], aoi[1], aoi[2], aoi[3])

        else:
            raise TypeError(f"aoi must be of type string or tuple")

        return aoi

    def query_metadata(self, platform, date, aoi, cloud_cover=None):
        """This method queries satellite image metadata from data source.

        :param platform: image platform (<enum 'Platform'>).
        :param date: Date from - to (String or Datetime tuple). Expects a tuple of (start, end), e.g.
            (yyyyMMdd, yyyy-MM-ddThh:mm:ssZ, NOW, NOW-<n>DAY(S), HOUR(S), MONTH(S), etc.)
        :param aoi: Area of interest as GeoJson file or bounding box tuple with lat lon coordinates (String, Tuple).
        :param cloud_cover: Percent cloud cover scene from - to (Integer tuple).
        :returns: Metadata of products that match query criteria (List of GeoJSON-like mappings).
        """
        if self.src == Datahub.file:
            raise NotImplementedError("File metadata query not yet supported.")

        elif self.src == Datahub.EarthExplorer:
            try:
                # query Earthexplorer for metadata
                bbox = self._prep_aoi(aoi).bounds
                kwargs = {}
                if cloud_cover:
                    kwargs["max_cloud_cover"] = cloud_cover[1]
                meta_src = self.api.search(
                    dataset=platform.value,
                    bbox=[bbox[1], bbox[0], bbox[3], bbox[2]],
                    start_date=sentinelsat.format_query_date(date[0]),
                    end_date=sentinelsat.format_query_date(date[1]),
                    max_results=10000,
                    **kwargs,
                )
            except Exception as e:
                raise Exception(
                    f"{traceback.format_exc()} Could not execute query to EarthExplorer. Check your query parameters. "
                    f"The following Exception was raised: {e}."
                )

        elif self.src == Datahub.Scihub:
            try:
                # query Scihub for metadata
                kwargs = {}
                if cloud_cover and platform != platform.Sentinel1:
                    kwargs["cloudcoverpercentage"] = cloud_cover
                meta_src = self.api.query(
                    area=self._prep_aoi(aoi).wkt,
                    date=date,
                    platformname=platform.value,
                    **kwargs,
                )
                meta_src = self.api.to_geojson(meta_src)["features"]
            except Exception as e:
                raise Exception(
                    f"{traceback.format_exc()} Could not execute query to Scihub. Check your query parameters."
                    f"The following Exception was raised: {e}."
                )

        try:
            # construct list of harmonized metadata
            meta = []
            for m in meta_src:
                meta.append(self.construct_metadata(meta_src=m))
            return meta
        except Exception as e:
            raise Exception(
                f"{traceback.format_exc()} Could not convert metadata to custom metadata. "
                f"The following Exception was raised: {e}."
            )

    def construct_metadata(self, meta_src):
        """This method constructs metadata that is harmonized across different satellite image sources and
        maps it into __geo_interface__ https://gist.github.com/sgillies/2217756
        Example Metadata: tests/testfiles/S2A_MSIL2A_20200221T102041_N0214_R065_T32UQC_20200221T120618.json

        :param meta_src: Source metadata (GeoJSON-like mapping)
        :returns: Harmonized metadata (GeoJSON-like mapping)
        """
        if self.src == Datahub.file:
            raise NotImplementedError("File metadata construction not yet supported.")

        elif self.src == Datahub.EarthExplorer:
            prop = {}
            prop["id"] = meta_src["displayId"]
            prop["platformname"] = Platform(
                meta_src["dataAccessUrl"][
                    meta_src["dataAccessUrl"].find("dataset_name=")
                    + len("dataset_name=") : meta_src["dataAccessUrl"].rfind("&ordered=")
                ]
            ).name
            prop["producttype"] = "L1TP"
            prop["orbitdirection"] = "DESCENDING"
            prop["orbitnumber"] = meta_src["summary"][
                meta_src["summary"].find("Path: ") + len("Path: ") : meta_src["summary"].rfind(", Row: ")
            ]
            prop["relativeorbitnumber"] = meta_src["summary"][meta_src["summary"].find("Row: ") + len("Row: ") :]
            prop["acquisitiondate"] = meta_src["acquisitionDate"]
            prop["ingestiondate"] = meta_src["modifiedDate"]
            prop["processingdate"] = ""
            prop["processingsteps"] = ""
            prop["processingversion"] = ""
            prop["bandlist"] = [{}]
            try:
                prop["cloudcoverpercentage"] = round(meta_src["cloudCover"], 2)
            except Exception:
                prop["cloudcoverpercentage"] = ""
            prop["format"] = "GeoTIFF"
            prop["size"] = ""
            prop["srcid"] = meta_src["displayId"]
            prop["srcurl"] = meta_src["dataAccessUrl"]
            prop["srcuuid"] = meta_src["entityId"]
            geom = meta_src["spatialFootprint"]

        elif self.src == Datahub.Scihub:
            prop = {}
            prop["id"] = meta_src["properties"]["identifier"]
            prop["platformname"] = Platform(meta_src["properties"]["platformname"]).name
            prop["producttype"] = meta_src["properties"]["producttype"]
            prop["orbitdirection"] = meta_src["properties"]["orbitdirection"]
            prop["orbitnumber"] = meta_src["properties"]["orbitnumber"]
            prop["relativeorbitnumber"] = meta_src["properties"]["relativeorbitnumber"]
            prop["acquisitiondate"] = meta_src["properties"]["beginposition"]
            prop["ingestiondate"] = meta_src["properties"]["ingestiondate"]
            prop["processingdate"] = ""
            prop["processingsteps"] = ""
            prop["processingversion"] = ""
            prop["bandlist"] = [{}]
            try:
                prop["cloudcoverpercentage"] = round(meta_src["properties"]["cloudcoverpercentage"], 2)
            except Exception:
                prop["cloudcoverpercentage"] = ""
            prop["format"] = meta_src["properties"]["format"]
            prop["size"] = meta_src["properties"]["size"]
            prop["srcid"] = meta_src["properties"]["identifier"]
            prop["srcurl"] = meta_src["properties"]["link"]
            prop["srcuuid"] = meta_src["properties"]["uuid"]
            geom = meta_src["geometry"]

        return geometry.mapping(_GeoInterface({"type": "Feature", "properties": prop, "geometry": geom}))

    def filter_metadata(self, meta, filter_dict):
        """This method filters metadata as returned by query_metadata() based on filter_dict.

        :param meta: Metadata of product(s) (List of GeoJSON-like mappings).
        :param filter_dict: Key value pair to use as filter e.g. {"producttype": "S2MSI1C"} (Dictionary).
        :returns: Metadata of products that match filter criteria (List of GeoJSON-like mappings).
        """
        m = []
        k = list(filter_dict.keys())
        for i in range(len(meta)):
            if filter_dict[k[0]] == meta[i]["properties"][k[0]]:
                m.append(meta[i])
        return m

    def download_metadata(self, meta, target_dir):
        """This method writes metadata as returned by query_metadata() to file.

        :param metadata: Metadata of product(s) (GeoJSON-like mapping).
        :param target_dir: Target directory that holds the downloaded metadata (String)
        """
        for i in range(len(meta)):
            with open(os.path.join(target_dir, meta[i]["properties"]["srcid"] + ".json"), "w") as f:
                json.dump(meta[i], f)

    def download_image(self, platform, product_uuid, target_dir):
        """This method downloads satellite image data to a target directory for a specific product_id.
        Incomplete downloads are continued and complete files are skipped.

        :param platform: image platform (<enum 'Platform'>).
        :param product_uuid: UUID of the satellite image product (String).
        :param target_dir: Target directory that holds the downloaded images (String)
        """
        if self.src == Datahub.file:
            logger.warning(f"download_image not supported for {self.src}.")

        elif self.src == Datahub.EarthExplorer:
            try:
                # query EarthExplorer for srcid of product
                meta_src = self.api.request("metadata", **{"datasetName": platform.value, "entityIds": [product_uuid],},)
                product_srcid = meta_src[0]["displayId"]
                # download data from AWS
                # landsatxplore is great for metadata search on EE but download via EE is slow. pylandsat is great
                # for fast download from AWS but does not provide good metadata search. Here we combine their benefits.
                product = Product(product_srcid)
                product.download(out_dir=target_dir, progressbar=False)
            except Exception as e:
                raise Exception(
                    f"{traceback.format_exc()} Could not download data through {self.src} (stored on AWS). "
                    f"The following Exception was raised: {e}."
                )
            try:
                # compress download directory and remove original files
                pack(
                    os.path.join(target_dir, product_srcid), root_dir=os.path.join(target_dir, product_srcid),
                )
                shutil.rmtree(os.path.join(target_dir, product_srcid))
            except Exception as e:
                raise Exception(
                    f"{traceback.format_exc()} Could not download data through {self.src}. "
                    f"This Exception was raised: {e}."
                )

        elif self.src == Datahub.Scihub:
            try:
                self.api.download(product_uuid, target_dir, checksum=True)
            except Exception as e:
                raise Exception(
                    f"{traceback.format_exc()} Could not download data through {self.src}. "
                    f"This Exception was raised: {e}."
                )

    def download_quicklook(self, platform, product_uuid, target_dir):
        """This method downloads a quicklook of the satellite image to a target directory for a specific product_id.
        It performs a very rough geocoding of the quicklooks by shifting the image to the location of the footprint.

        :param platform: image platform (<enum 'Platform'>).
        :param product_uuid: UUID of the satellite image product (String).
        :param target_dir: Target directory that holds the downloaded images (String)
        """
        if self.src == Datahub.file:
            raise NotImplementedError(f"download_quicklook not supported for {self.src}.")

        elif self.src == Datahub.EarthExplorer:
            try:
                # query EarthExplorer for url, srcid and bounds of product
                meta_src = self.api.request("metadata", **{"datasetName": platform.value, "entityIds": [product_uuid],},)
                url = meta_src[0]["browseUrl"]
                bounds = geometry.shape(meta_src[0]["spatialFootprint"]).bounds
                product_srcid = meta_src[0]["displayId"]
            except Exception as e:
                logger.warning(
                    f"{traceback.format_exc()} Could not download and save quicklook. "
                    f"This Exception was raised: {e}."
                )

        elif self.src == Datahub.Scihub:
            try:
                # query Scihub for url, srcid and bounds of product
                meta_src = self.api.get_product_odata(product_uuid)
                url = "https://scihub.copernicus.eu/apihub/odata/v1/Products('{}')/Products('Quicklook')/$value".format(
                    product_uuid
                )
                bounds = wkt.loads(meta_src["footprint"]).bounds
                product_srcid = meta_src["title"]
            except Exception as e:
                logger.warning(
                    f"{traceback.format_exc()} Could not download and save quicklook. This Exception was raised: {e}."
                )

        # download quicklook and crop no-data borders
        response = requests.get(url, auth=(self.user, self.pw))
        quicklook = np.asarray(Image.open(BytesIO(response.content)))
        # use threshold of 50 to overcome noise in JPEG compression
        xs, ys, zs = np.where(quicklook >= 50)
        quicklook = quicklook[min(xs):max(xs)+1, min(ys):max(ys)+1, min(zs):max(zs)+1]
        Image.fromarray(quicklook).save(os.path.join(target_dir, product_srcid + ".jpg"))

        # geocode quicklook
        quicklook_size = (quicklook.shape[1], quicklook.shape[0])
        dist_x = geometry.Point(bounds[0], bounds[1]).distance(geometry.Point(bounds[2], bounds[1])) / quicklook_size[0]
        dist_y = geometry.Point(bounds[0], bounds[1]).distance(geometry.Point(bounds[0], bounds[3])) / quicklook_size[1]
        ul_x, ul_y = bounds[0], bounds[3]
        with open(os.path.join(os.path.join(target_dir, product_srcid + ".jpgw")), "w") as out_file:
            out_file.write(str(dist_x) + "\n")
            out_file.write(str(0.0) + "\n")
            out_file.write(str(0.0) + "\n")
            out_file.write(str(-dist_y) + "\n")
            out_file.write(str(ul_x) + "\n")
            out_file.write(str(ul_y) + "\n")

    def close(self):
        """closes connection to or logs out of Datahub"""
        if self.src == Datahub.EarthExplorer:
            self.api.logout()
        elif self.src == Datahub.Scihub:
            self.api.session.close()
        else:
            pass

    def __exit__(self, exc_type, exc_val, exc_tb):
        self.close()


class _GeoInterface(object):
    def __init__(self, d):
        self.__geo_interface__ = d


if __name__ == "__main__":
    pass
