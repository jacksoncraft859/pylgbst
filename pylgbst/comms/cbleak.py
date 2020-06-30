import asyncio
import logging
import queue
import threading
import time

from bleak import BleakClient, discover

from pylgbst.comms import Connection, MOVE_HUB_HW_UUID_CHAR

log = logging.getLogger('comms-bleak')

# Queues to handle request / responses. Acts as a buffer between API and async BLE driver
resp_queue = queue.Queue()
req_queue = queue.Queue()




class BleakConnection2(Connection):
    """
    :type _client: BleakClient
    """

    def __init__(self) -> None:
        super().__init__()
        self._abort = False
        self._loop = asyncio.new_event_loop()
        asyncio.set_event_loop(self._loop)
        self._client = None  # noqa
        self._notify_queue = queue.Queue()

    def connect(self, hub_mac=None, hub_name=None):
        #logging.getLogger('bleak.backends.dotnet.client').setLevel(logging.WARNING)
        #logging.getLogger('bleak.backends.bluezdbus.client').setLevel(logging.WARNING)
        #logging.getLogger('bleak.backends.dotnet.discovery').setLevel(logging.WARNING)
        #logging.getLogger('bleak.backends.bluezdbus.discovery').setLevel(logging.WARNING)

        log.info("Discovering devices... Press green button on Hub")
        for i in range(0, 30):
            devices = self._loop.run_until_complete(discover(timeout=1))
            log.debug("Devices: %s", devices)
            for dev in devices:
                log.debug(dev)
                address = dev.address
                name = dev.name
                if self._is_device_matched(address, name, hub_mac, hub_name):
                    log.info('Device matched: %r', dev)
                    device = dev
                    break
            else:
                continue

            break
        else:
            raise ConnectionError('Device not found.')

        self._client = BleakClient(device.address, self._loop)
        status = self._loop.run_until_complete(self._client.connect())
        log.debug('Connection status: %s', status)

        def enqueue(handle, data):
            log.debug("Put into queue: %s", data)
            self._notify_queue.put((handle, data))

        self._loop.run_until_complete(self._client.start_notify(MOVE_HUB_HW_UUID_CHAR, enqueue))

        return self

    def disconnect(self):
        self._abort = True
        if self.is_alive():
            log.debug("Disconnecting bleak connection")
            self._loop.run_until_complete(self._client.disconnect())

    def is_alive(self):
        log.debug("Checking if bleak conn is alive")
        return self._loop.run_until_complete(self._client.is_connected())

    def write(self, handle, data):
        desc = self._client.services.get_descriptor(handle)

        if not isinstance(data, bytearray):
            data = bytearray(data)

        if desc is None:
            # dedicated handle not found, try to send by using LEGO Move Hub default characteristic
            self._loop.run_until_complete(self._client.write_gatt_char(MOVE_HUB_HW_UUID_CHAR, data))
        else:
            self._loop.run_until_complete(self._client.write_gatt_char(desc.characteristic_uuid, data))

    def set_notify_handler(self, handler):
        def _processing():
            while not self._abort:
                handle, data = self._notify_queue.get(block=True)
                handler(handle, data)

            log.info("Processing thread has exited")

        threading.Thread(target=_processing, daemon=True).start()


class BleakDriver(object):
    """Driver that provides interface between API and Bleak."""

    def __init__(self, hub_mac=None, hub_name=None):
        """
        Initialize new object of Bleak Driver class.

        :param hub_mac: Optional Lego HUB MAC to connect to.
        """
        self.hub_mac = hub_mac
        self.hub_name = hub_name
        self._handler = None
        self._abort = False
        self._connection_thread = None
        self._processing_thread = None

    def set_notify_handler(self, handler):
        """
        Set handler function used to communicate with an API.

        :param handler: Handler function called by driver when received data
        :return: None
        """
        self._handler = handler

    def enable_notifications(self):
        """
        Enable notifications, in our cases starts communication threads.

        We cannot do this earlier, because API need to fist set notification handler.
        :return: None
        """
        self._connection_thread = threading.Thread(target=lambda: asyncio.run(self._bleak_thread()))
        self._connection_thread.daemon = True
        self._connection_thread.start()

        self._processing_thread = threading.Thread(target=self._processing)
        self._processing_thread.daemon = True
        self._processing_thread.start()

    async def _bleak_thread(self):
        bleak = BleakConnection()
        await bleak.connect(self.hub_mac, self.hub_name)
        await bleak.set_notify_handler(self._safe_handler)
        # After connecting, need to send any data or hub will drop the connection,
        # below command is Advertising name request update
        await bleak.write_char(MOVE_HUB_HW_UUID_CHAR, bytearray([0x05, 0x00, 0x01, 0x01, 0x05]))
        while not self._abort:
            await asyncio.sleep(0.1)
            if req_queue.qsize() != 0:
                data = req_queue.get()
                await bleak.write(data[0], data[1])

        log.info("Communications thread has exited")

    @staticmethod
    def _safe_handler(handler, data):
        resp_queue.put((handler, data))

    def _processing(self):
        while not self._abort:
            if resp_queue.qsize() != 0:
                msg = resp_queue.get()
                self._handler(msg[0], msg[1])

            time.sleep(0.01)
        log.info("Processing thread has exited")

    def write(self, handle, data):
        """
        Send data to given handle number.

        :param handle: Handle number that will be translated into characteristic uuid
        :param data: data to send
        :raises ConnectionError" When internal threads are not working
        :return: None
        """
        if not self._connection_thread.is_alive() or not self._processing_thread.is_alive():
            raise ConnectionError('Something went wrong, communication threads not functioning.')

        req_queue.put((handle, data))

    def disconnect(self):
        """
        Disconnect and stops communication threads.

        :return: None
        """
        self._abort = True

    def is_alive(self):
        """
        Indicate whether driver is functioning or not.

        :return: True if driver is functioning; False otherwise.
        """
        if self._connection_thread is not None and self._processing_thread is not None:
            return self._connection_thread.is_alive() and self._processing_thread.is_alive()
        else:
            return False


class BleakConnection(Connection):
    """Bleak driver for communicating with BLE device."""

    def __init__(self):
        """Initialize new instance of BleakConnection class."""
        super().__init__(self)
        self.loop = asyncio.get_event_loop()

        self._device = None
        self._client = None
        logging.getLogger('bleak.backends.dotnet.client').setLevel(logging.WARNING)
        logging.getLogger('bleak.backends.bluezdbus.client').setLevel(logging.WARNING)

    async def connect(self, hub_mac=None, hub_name=None):
        """
        Connect to device.

        :param hub_mac: Optional Lego HUB MAC to connect to.
        :raises ConnectionError: When cannot connect to given MAC or name matching fails.
        :return: None
        """
        log.info("Discovering devices... Press green button on Hub")
        devices = await discover(timeout=10)
        log.debug("Devices: %s", devices)

        for dev in devices:
            log.debug(dev)
            address = dev.address
            name = dev.name
            if self._is_device_matched(address, name, hub_mac, hub_name):
                log.info('Device matched: %r', dev)
                self._device = dev
                break
        else:
            raise ConnectionError('Device not found.')

        self._client = BleakClient(self._device.address, self.loop)
        status = await self._client.connect()
        log.debug('Connection status: {status}'.format(status=status))

    async def write(self, handle, data):
        """
        Send data to given handle number.

        If handle cannot be found in service description, hardcoded LEGO uuid will be used.
        :param handle: Handle number that will be translated into characteristic uuid
        :param data: data to send
        :return: None
        """
        log.debug('Request: {handle} {payload}'.format(handle=handle, payload=[hex(x) for x in data]))
        desc = self._client.services.get_descriptor(handle)

        if not isinstance(data, bytearray):
            data = bytearray(data)

        if desc is None:
            # dedicated handle not found, try to send by using LEGO Move Hub default characteristic
            await self._client.write_gatt_char(MOVE_HUB_HW_UUID_CHAR, data)
        else:
            await self._client.write_gatt_char(desc.characteristic_uuid, data)

    async def write_char(self, characteristic_uuid, data):
        """
        Send data to given handle number.

        :param characteristic_uuid: Characteristic uuid used to send data
        :param data: data to send
        :return: None
        """
        await self._client.write_gatt_char(characteristic_uuid, data)

    async def set_notify_handler(self, handler):
        """
        Set notification handler.

        :param handler: Handle function to be called when receive any data.
        :return: None
        """

        def c(handle, data):
            log.debug('Response: {handle} {payload}'.format(handle=handle, payload=[hex(x) for x in data]))
            handler(handle, data)

        await self._client.start_notify(MOVE_HUB_HW_UUID_CHAR, c)

    def is_alive(self):
        """
        To keep compatibility with the driver interface.

        This method does nothing.
        :return: None.
        """
        return self._client.is_connected()
