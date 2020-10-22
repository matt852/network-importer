"""
(c) 2020 Network To Code

Licensed under the Apache License, Version 2.0 (the "License");
you may not use this file except in compliance with the License.
You may obtain a copy of the License at
  http://www.apache.org/licenses/LICENSE-2.0
Unless required by applicable law or agreed to in writing, software
distributed under the License is distributed on an "AS IS" BASIS,
WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
See the License for the specific language governing permissions and
limitations under the License.
"""
from typing import Optional
import logging

import pynetbox

import network_importer.config as config  # pylint: disable=import-error
from network_importer.models import (  # pylint: disable=import-error
    Site,
    Device,
    Interface,
    IPAddress,
    Cable,
    Prefix,
    Vlan,
)


LOGGER = logging.getLogger("network-importer")


class NetboxSite(Site):
    remote_id: Optional[int]


class NetboxDevice(Device):
    remote_id: Optional[int]
    primary_ip: Optional[str]


class NetboxInterface(Interface):
    remote_id: Optional[int]
    connected_endpoint_type: Optional[str]

    def translate_attrs_for_netbox(self, attrs):
        """Translate interface parameters into Netbox format

        Args:
            params (dict): Dictionnary of attributes/parameters of the object to translate

        Returns:
            dict: Netbox parameters
        """

        def convert_vlan_to_nid(vlan_uid):
            vlan = self.dsync.get(self.dsync.vlan, identifier=vlan_uid)
            if vlan:
                return vlan.remote_id
            return None

        nb_params = {}

        # Identify the id of the device this interface is attached to
        device = self.dsync.get(self.dsync.device, identifier=self.device_name)
        nb_params["device"] = device.remote_id
        nb_params["name"] = self.name

        if "is_lag" in attrs and attrs["is_lag"]:
            nb_params["type"] = "lag"
        elif "is_virtual" in attrs and attrs["is_virtual"]:
            nb_params["type"] = "virtual"
        else:
            nb_params["type"] = "other"

        if "mtu" in attrs:
            nb_params["mtu"] = attrs["mtu"]

        if "description" in attrs:
            nb_params["description"] = attrs["description"] or ""

        if "switchport_mode" in attrs and attrs["switchport_mode"] == "ACCESS":
            nb_params["mode"] = "access"
        elif "switchport_mode" in attrs and attrs["switchport_mode"] == "TRUNK":
            nb_params["switchport_mode"] = "tagged"

        # if is None:
        #     intf_properties["enabled"] = intf.active

        if config.SETTINGS.main.import_vlans not in [False, "no"]:
            if "mode" in attrs and attrs["mode"] in ["TRUNK", "ACCESS"] and attrs["access_vlan"]:
                nb_params["untagged_vlan"] = convert_vlan_to_nid(attrs["access_vlan"])
            elif "mode" in attrs and attrs["mode"] in ["TRUNK", "ACCESS"] and not attrs["access_vlan"]:
                nb_params["untagged_vlan"] = None

            if (
                "mode" in attrs
                and attrs["mode"] in ["TRUNK", "L3_SUB_VLAN"]
                and "allowed_vlans" in attrs
                and attrs["allowed_vlans"]
            ):
                nb_params["tagged_vlans"] = [convert_vlan_to_nid(vlan) for vlan in attrs["allowed_vlans"]]
            elif (
                "mode" in attrs
                and attrs["mode"] in ["TRUNK", "L3_SUB_VLAN"]
                and ("allowed_vlans" not in attrs or not attrs["allowed_vlans"])
            ):
                nb_params["tagged_vlans"] = []

        if "is_lag_member" in attrs and attrs["is_lag_member"]:
            # TODO add checks to ensure the parent interface is present and has a remote id
            parent_interface = self.dsync.get(self.dsync.interface, identifier=attrs["parent"])
            nb_params["lag"] = parent_interface.remote_id

        elif "is_lag_member" in attrs and not attrs["is_lag_member"]:
            nb_params["lag"] = None

        return nb_params

    @classmethod
    def create(cls, dsync: "DSync", ids: dict, attrs: dict) -> Optional["DSyncModel"]:
        """Create an interface object in Netbox.

        Args:
            dsync: The master data store for other DSyncModel instances that we might need to reference
            ids: Dictionary of unique-identifiers needed to create the new object
            attrs: Dictionary of additional attributes to set on the new object

        Returns:
            NetboxInterface: DSync object newly created
        """

        item = super().create(ids=ids, dsync=dsync, attrs=attrs)
        nb_params = item.translate_attrs_for_netbox(attrs)

        try:
            intf = dsync.netbox.dcim.interfaces.create(**nb_params)
            LOGGER.debug("Created interface %s (%s) in NetBox", intf.name, intf.id)
        except pynetbox.core.query.RequestError as exc:
            LOGGER.warning(
                "Unable to create interface %s on %s in %s (%s)", ids["name"], ids["device_name"], dsync.name, exc.error
            )
            return

        item.remote_id = intf.id
        return item

    def update(self, attrs: dict) -> Optional["DSyncModel"]:
        """Update an interface object in Netbox.

        Args:
            attrs: Dictionary of attributes to update on the object

        Returns:
            DSyncModel: this instance, if all data was successfully updated.
            None: if data updates failed in such a way that child objects of this model should not be modified.

        Raises:
            ObjectNotUpdated: if an error occurred.
        """

        current_attrs = self.get_attrs()

        if attrs == current_attrs:
            return self

        nb_params = self.translate_attrs_for_netbox(attrs)

        try:
            intf = self.dsync.netbox.dcim.interfaces.get(self.remote_id)
            intf.update(data=nb_params)
            LOGGER.info("Updated Interface %s %s (%s) in NetBox", self.device_name, self.name, self.remote_id)
        except pynetbox.core.query.RequestError as exc:
            LOGGER.warning(
                "Unable to update interface %s on %s in %s (%s)",
                self.name,
                self.device_name,
                self.dsync.name,
                exc.error,
            )
            return

        return super().update(attrs)

    def delete(self) -> Optional["DSyncModel"]:
        """Delete an interface object in Netbox.

        Returns:
            NetboxInterface: DSync object
        """
        # Check if the interface has some Ips, check if it is the management interface
        if self.ips:
            dev = self.dsync.get(self.dsync.device, identifier=self.device_name)
            if dev.primary_ip and dev.primary_ip in self.ips:
                LOGGER.warning(
                    "Unable to delete interface %s on %s, because it's currently the management interface",
                    self.name,
                    dev.name,
                )
                return self
        try:
            intf = self.dsync.netbox.dcim.interfaces.get(self.remote_id)
            intf.delete()
        except pynetbox.core.query.RequestError as exc:
            LOGGER.warning(
                "Unable to delete Interface %s on %s in %s (%s)",
                self.name,
                self.device_name,
                self.dsync.name,
                exc.error,
            )

        super().delete()
        return self


class NetboxIPAddress(IPAddress):
    remote_id: Optional[int]

    @classmethod
    def create(cls, dsync: "DSync", ids: dict, attrs: dict) -> Optional["DSyncModel"]:
        """Create an IP address in Netbox, if the name of a valid interface is provided the interface will be assigned to the interface.

        Returns:
            NetboxIPAddress: DSync object
        """
        interface = None
        if "interface_name" in attrs and "device_name" in attrs:
            interface = dsync.get(
                dsync.interface, identifier=dict(device_name=attrs["device_name"], name=attrs["interface_name"])
            )

        try:
            if interface:
                ip_address = dsync.netbox.ipam.ip_addresses.create(
                    address=ids["address"], interface=interface.remote_id
                )
            else:
                ip_address = dsync.netbox.ipam.ip_addresses.create(address=ids["address"])
        except pynetbox.core.query.RequestError as exc:
            LOGGER.warning("Unable to create the ip address %s in %s (%s)", ids["address"], dsync.name, exc.error)
            return

        LOGGER.debug("Created IP %s (%s) in NetBox", ip_address.address, ip_address.id)

        item = super().create(ids=ids, dsync=dsync, attrs=attrs)
        item.remote_id = ip_address.id

        return item

    def delete(self) -> Optional["DSyncModel"]:
        """Delete an IP address in NetBox

        Returns:
            NetboxIPAddress: DSync object
        """
        if self.device_name:
            dev = self.dsync.get(self.dsync.device, identifier=self.device_name)
            if dev.primary_ip == self.address:
                LOGGER.warning(
                    "Unable to delete IP Address %s on %s, because it's currently the management IP address",
                    self.address,
                    self.device_name,
                )
                return

        try:
            ipaddr = self.dsync.netbox.ipam.ip_addresses.get(self.remote_id)
            ipaddr.delete()
        except pynetbox.core.query.RequestError as exc:
            LOGGER.warning(
                "Unable to delete IP Address %s on %s in %s (%s)",
                self.address,
                self.device_name,
                self.dsync.name,
                exc.error,
            )
            return

        super().delete()
        return self


class NetboxPrefix(Prefix):
    remote_id: Optional[int]

    def translate_attrs_for_netbox(self, attrs):
        """Translate prefix parameters into Netbox format

        Args:
            params (dict): Dictionnary of attributes/parameters of the object to translate

        Returns:
            dict: Netbox parameters
        """
        nb_params = {"prefix": self.prefix, "status": "active"}

        site = self.dsync.get(self.dsync.site, identifier=self.site_name)
        nb_params["site"] = site.remote_id

        if "vlan" in attrs:
            vlan = self.dsync.get(self.dsync.vlan, identifier=attrs["vlan"])
            if vlan.remote_id:
                nb_params["vlan"] = vlan.remote_id

        return nb_params

    @classmethod
    def create(cls, dsync: "DSync", ids: dict, attrs: dict) -> Optional["DSyncModel"]:
        """Create a Prefix in NetBox

        Returns:
            NetboxPrefix: DSync object
        """

        item = super().create(ids=ids, dsync=dsync, attrs=attrs)
        nb_params = item.translate_attrs_for_netbox(attrs)

        try:
            prefix = dsync.netbox.ipam.prefixes.create(**nb_params)
            LOGGER.debug("Created Prefix %s (%s) in NetBox", prefix.prefix, prefix.id)
        except pynetbox.core.query.RequestError as exc:
            LOGGER.warning("Unable to create Prefix %s in %s (%s)", ids["prefix"], dsync.name, exc.error)
            return

        item.remote_id = prefix.id

        return item

    def update(self, attrs: dict) -> Optional["DSyncModel"]:
        """Update a Prefix object in Netbox.

        Args:
            attrs: Dictionary of attributes to update on the object

        Returns:
            DSyncModel: this instance, if all data was successfully updated.
            None: if data updates failed in such a way that child objects of this model should not be modified.

        Raises:
            ObjectNotUpdated: if an error occurred.
        """

        current_attrs = self.get_attrs()

        if attrs == current_attrs:
            return self

        nb_params = self.translate_attrs_for_netbox(attrs)

        try:
            prefix = self.dsync.netbox.ipam.prefixes.get(self.remote_id)
            prefix.update(data=nb_params)
            LOGGER.info("Updated Prefix %s (%s) in NetBox", self.prefix, self.remote_id)
        except pynetbox.core.query.RequestError as exc:
            LOGGER.warning(
                "Unable to update perfix %s in %s (%s)", self.prefix, self.dsync.name, exc.error,
            )
            return

        return super().update(attrs)


class NetboxVlan(Vlan):
    remote_id: Optional[int]

    @classmethod
    def create(cls, dsync: "DSync", ids: dict, attrs: dict) -> Optional["DSyncModel"]:
        """Create new Vlan in NetBox

        Returns:
            NetboxVlan: DSync object
        """
        site = dsync.get(dsync.site, identifier=ids["site_name"])

        if "name" in attrs and attrs["name"]:
            vlan_name = attrs["name"]
        else:
            vlan_name = f"vlan-{ids['vid']}"

        try:
            vlan = dsync.netbox.ipam.vlans.create(vid=ids["vid"], name=vlan_name, site=site.remote_id)
        except pynetbox.core.query.RequestError as exc:
            LOGGER.warning("Unable to create Vlan %s in %s (%s)", ids, dsync.name, exc.error)
            return

        item = super().create(ids=ids, dsync=dsync, attrs=attrs)
        item.remote_id = vlan.id

        return item

    def update(self, attrs: dict) -> Optional["DSyncModel"]:
        """Update new Vlan in NetBox

        Returns:
            NetboxVlan: DSync object
        """

        try:
            vlan = self.dsync.netbox.ipam.vlans.get(self.remote_id)
            vlan.update(data={"name": attrs["name"]})
        except pynetbox.core.query.RequestError as exc:
            LOGGER.warning("Unable to update Vlan %s in %s (%s)", self.get_unique_id(), self.dsync.name, exc.error)
            return

        return super().update(attrs)


class NetboxCable(Cable):
    remote_id: Optional[int]
    termination_a_id: Optional[int]
    termination_z_id: Optional[int]

    @classmethod
    def create(cls, dsync: "DSync", ids: dict, attrs: dict) -> Optional["DSyncModel"]:
        """Create a Cable in NetBox

        Returns:
            NetboxCable: DSync object
        """
        interface_a = dsync.get(
            dsync.interface, identifier=dict(device_name=ids["device_a_name"], name=ids["interface_a_name"])
        )
        interface_z = dsync.get(
            dsync.interface, identifier=dict(device_name=ids["device_z_name"], name=ids["interface_z_name"])
        )

        if not interface_a:
            interface_a = dsync.get_intf_from_netbox(
                device_name=ids["device_a_name"], intf_name=ids["interface_a_name"]
            )

        if not interface_z:
            interface_z = dsync.get_intf_from_netbox(
                device_name=ids["device_z_name"], intf_name=ids["interface_z_name"]
            )

        if not interface_a or not interface_z:
            return

        if interface_a.connected_endpoint_type:
            LOGGER.info(
                "Unable to create Cable in %s, port %s %s is already connected",
                dsync.name,
                ids["device_a_name"],
                ids["interface_a_name"],
            )
            return

        if interface_z.connected_endpoint_type:
            LOGGER.info(
                "Unable to create Cable in %s, port %s %s is already connected",
                dsync.name,
                ids["device_z_name"],
                ids["interface_z_name"],
            )
            return

        try:
            cable = dsync.netbox.dcim.cables.create(
                termination_a_type="dcim.interface",
                termination_b_type="dcim.interface",
                termination_a_id=interface_a.remote_id,
                termination_b_id=interface_z.remote_id,
            )
        except pynetbox.core.query.RequestError as exc:
            LOGGER.warning("Unable to create Cable %s in %s (%s)", ids, dsync.name, exc.error)
            return

        interface_a.connected_endpoint_type = "dcim.interface"
        interface_z.connected_endpoint_type = "dcim.interface"

        item = super().create(ids=ids, dsync=dsync, attrs=attrs)
        LOGGER.info("Created Cable %s (%s) in NetBox", item.get_unique_id(), cable.id)
        item.remote_id = cable.id

        return item

    def delete(self):  #  pylint: disable=unused-argument
        """Delete a Cable in NetBox

        Returns:
            NetboxCable: DSync object
        """
        try:
            cable = self.dsync.netbox.dcim.cables.get(self.remote_id)
            cable.delete()
        except pynetbox.core.query.RequestError as exc:
            LOGGER.warning("Unable to delete the cable %s in %s (%s)", self.get_unique_id(), self.dsync.name, exc.error)

        super().delete()
        return self
