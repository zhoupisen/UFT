#!/usr/bin/env python
# encoding: utf-8
"""Description: pgem parallel test on UFT test fixture.
Currently supports 4 duts in parallel.
"""

__version__ = "0.1"
__author__ = "@boqiling"
__all__ = ["Channel", "ChannelStates"]

from UFT.devices import pwr, load, aardvark
from UFT.models import DUT_STATUS, DUT, Cycle
from UFT.backend import load_config, load_test_item
from UFT.backend.session import SessionManager
from UFT.config import *
import threading
from Queue import Queue
import logging
import time
import math
logger = logging.getLogger(__name__)


#def get_sensor_status():
#    """
#    get sensor status of each DUT present.
#    :return: list of 1 and 0, 1 for present, 0 for not.
#    """
#    result = []
#    for i in range(TOTAL_SLOTNUM):
#        a = multimeter.read_analog_ch(i)
#        if(a > 8):
#            result.append(0)
#        elif(a < 1):
#            result.append(1)
#        else:
#            raise RuntimeError("unvalid sensor status.")
#    logger.info("dut list: {0}".format(result))
#    return result


class ChannelStates(object):
    EXIT = -1
    INIT = 0x0A
    LOAD_DISCHARGE = 0x0C
    CHARGE = 0x0E
    PROGRAM_VPD = 0x0F
    CHECK_CAPACITANCE = 0x1A
    CHECK_ENCRYPTED_IC = 0x1B
    CHECK_TEMP = 0x1C
    DUT_DISCHARGE = 0x1D


class Channel(threading.Thread):
    # aardvark
    adk = aardvark.Adapter(portnum=ADK_PORT)
    # setup load
    ld = load.DCLoad(port=LD_PORT, timeout=LD_DELAY)
    # setup main power supply
    ps = pwr.PowerSupply()


    def __init__(self, name, barcode_list, channel_id=0):
        """initialize channel
        :param name: thread name
        :param barcode_list: list of 2D barcode of dut.
        :param channel_id: channel ID, from 0 to 7
        :return: None
        """
        # channel number for mother board.
        # 8 mother boards can be stacked from 0 to 7.
        # use 1 motherboard in default.
        self.channel = channel_id

        # setup dut_list
        self.dut_list = []
        self.config_list = []
        self.barcode_list = barcode_list

        # progress bar, 0 to 100
        self.progressbar = 0

        # counter, to calculate charge and discharge time based on interval
        self.counter = 0

        # pre-discharge current, default to 0.8A
        self.current = 0.8

        # exit flag and queue for threading
        self.exit = False
        self.queue = Queue()


        super(Channel, self).__init__(name=name)

    def init(self):
        """ hardware initialize in when work loop starts.
        :return: None.
        """
        # setup dut_list
        for i, bc in enumerate(self.barcode_list):
            if bc != "":
                # dut is present
                dut = PGEM_MODEL(device=self.adk,
                                 slot=i,
                                 barcode=bc)
                dut.status = DUT_STATUS.Idle
                self.dut_list.append(dut)
                dut_config = load_config("sqlite:///" + CONFIG_DB,
                                         dut.partnumber, dut.revision)
                self.config_list.append(dut_config)
            else:
                # dut is not loaded on fixture
                self.dut_list.append(None)
                self.config_list.append(None)

        # setup load
        for slot in range(TOTAL_SLOTNUM):
            self.ld.select_channel(slot)
            self.ld.input_off()
            self.ld.protect_on()
            self.ld.change_func(load.DCLoad.ModeCURR)

        # setup power supply
        self.ps.selectChannel(node=PS_ADDR, ch=PS_CHAN)

        if(self.ps.measureVolt() <= 0.1):
            # not powered.
            setting = {"volt": PS_VOLT, "curr": PS_CURR,
                       "ovp": PS_OVP, "ocp": PS_OCP}
            self.ps.set(setting)
            self.ps.activateOutput()
            time.sleep(1)
            volt = self.ps.measureVolt()
            curr = self.ps.measureCurr()
            assert (PS_VOLT-1) < volt < (PS_VOLT+1)
            assert curr >= 0

        # reset DUT
        self.reset_dut()

    def reset_dut(self):
        """disable all charge and self-discharge, enable auto-discharge.
        just like the dut is not present.
        :return: None
        """
        for dut in self.dut_list:
            if dut is not None:
                self.switch_to_dut(dut.slotnum)
                try:
                    # disable self discharge
                    dut.self_discharge(status=False)
                except:
                    #maybe dut has no power, doesn't response
                    pass

                # disable charge
                dut.charge(status=False)

                # enable auto discharge
                self.switch_to_mb()
                self.auto_discharge(slot=dut.slotnum, status=True)

                # empty the dut, one by one
                self.ld.select_channel(dut.slotnum)
                val = self.ld.read_volt()
                if(val > START_VOLT):
                    self.ld.set_curr(self.current)
                    self.ld.input_on()
                    dut.status = DUT_STATUS.Discharging
                while(val > START_VOLT):
                    val = self.ld.read_volt()
                    time.sleep(INTERVAL)
                self.ld.input_off()
                dut.status = DUT_STATUS.Idle

    def charge_dut(self):
        """charge
        """
        for dut in self.dut_list:
            if dut is None:
                continue
            config = load_test_item(self.config_list[dut.slotnum],
                                    "Charge")
            if(not config["enable"]):
                continue
            if(config["stoponfail"]) & (dut.status != DUT_STATUS.Idle):
                continue
            # disable auto discharge
            self.switch_to_mb()
            self.auto_discharge(slot=dut.slotnum, status=False)
            self.switch_to_dut(dut.slotnum)
            try:
                # disable self discharge
                dut.self_discharge(status=False)
            except aardvark.USBI2CAdapterException:
                # maybe dut has no power, doesn't response
                pass
            # start charge
            dut.charge(option=config, status=True)
            dut.status = DUT_STATUS.Charging

        all_charged = False
        self.counter = 0
        start_time = time.time()
        while(not all_charged):
            all_charged = True
            for dut in self.dut_list:
                if dut is None:
                    continue
                config = load_test_item(self.config_list[dut.slotnum],
                                        "Charge")
                if(not config["enable"]):
                    continue
                if(config["stoponfail"]) & \
                        (dut.status != DUT_STATUS.Charging):
                    continue
                self.switch_to_dut(dut.slotnum)

                this_cycle = Cycle()
                this_cycle.vin = self.ps.measureVolt()
                this_cycle.counter = self.counter
                this_cycle.time = time.time()
                try:
                    temperature = dut.check_temp()
                except aardvark.USBI2CAdapterException:
                    # temp ic not ready
                    temperature = 0
                this_cycle.temp = temperature
                this_cycle.state = "charge"
                self.counter += 1

                self.ld.select_channel(dut.slotnum)
                this_cycle.vcap = self.ld.read_volt()

                threshold = float(config["Threshold"].strip("aAvV"))
                max_chargetime = config["max"]
                min_chargetime = config["min"]

                if(this_cycle.vcap > threshold):
                    all_charged &= True
                    #dut.charge(status=False)
                    charge_time = this_cycle.time - start_time
                    dut.charge_time = charge_time
                    if(charge_time < min_chargetime):
                        dut.status = DUT_STATUS.Fail
                        dut.errormessage = "Charge Time Too Short."
                    elif(charge_time > max_chargetime):
                        dut.status = DUT_STATUS.Fail
                        dut.errormessage = "Charge Time Too Long."
                    else:
                        dut.status = DUT_STATUS.Idle    # pass
                else:
                    all_charged &= False
                dut.cycles.append(this_cycle)
                logger.info("dut: {0} status: {1} vcap: {2} "
                            "temp: {3} message: {4} ".
                            format(dut.slotnum, dut.status, this_cycle.vcap,
                                   this_cycle.temp, dut.errormessage))
            time.sleep(INTERVAL)

    def discharge_dut(self):
        """discharge
        """
        for dut in self.dut_list:
            if dut is None:
                continue
            config = load_test_item(self.config_list[dut.slotnum],
                                    "Discharge")
            if(not config["enable"]):
                continue
            if(config["stoponfail"]) & (dut.status != DUT_STATUS.Idle):
                continue
            # disable auto discharge
            self.switch_to_mb()
            self.auto_discharge(slot=dut.slotnum, status=False)
            # disable self discharge
            self.switch_to_dut(dut.slotnum)
            dut.self_discharge(status=False)
            # disable charge
            dut.charge(status=False)

            self.ld.select_channel(dut.slotnum)
            self.current = float(config["Current"].strip("aAvV"))
            self.ld.set_curr(self.current)  # set discharge current
            self.ld.input_on()
            dut.status = DUT_STATUS.Discharging

        # start discharge cycle
        all_discharged = False
        start_time = time.time()
        while(not all_discharged):
            all_discharged = True
            for dut in self.dut_list:
                if dut is None:
                    continue
                config = load_test_item(self.config_list[dut.slotnum],
                                        "Discharge")
                if(not config["enable"]):
                    continue
                if(config["stoponfail"]) & \
                        (dut.status != DUT_STATUS.Discharging):
                    continue
                self.switch_to_dut(dut.slotnum)

                this_cycle = Cycle()
                this_cycle.vin = self.ps.measureVolt()
                try:
                    temperature = dut.check_temp()
                except aardvark.USBI2CAdapterException:
                    # temp ic not ready
                    temperature = 0
                this_cycle.temp = temperature
                this_cycle.counter = self.counter
                this_cycle.time = time.time()

                this_cycle.state = "discharge"
                self.ld.select_channel(dut.slotnum)
                this_cycle.vcap = self.ld.read_volt()
                self.counter += 1

                threshold = float(config["Threshold"].strip("aAvV"))
                max_dischargetime = config["max"]
                min_dischargetime = config["min"]

                if(this_cycle.vcap < threshold):
                    all_discharged &= True
                    discharge_time = this_cycle.time - start_time
                    dut.discharge_time = discharge_time

                    self.ld.select_channel(dut.slotnum)
                    self.ld.input_off()
                    if(discharge_time < min_dischargetime):
                        dut.status = DUT_STATUS.Fail
                        dut.errormessage = "Discharge Time Too Short."
                    elif(discharge_time > max_dischargetime):
                        dut.status = DUT_STATUS.Fail
                        dut.errormessage = "Discharge Time Too Long."
                    else:
                        dut.status = DUT_STATUS.Idle
                else:
                    all_discharged &= False
                dut.cycles.append(this_cycle)
                logger.info("dut: {0} status: {1} vcap: {2} "
                            "temp: {3} message: {4} ".
                            format(dut.slotnum, dut.status, this_cycle.vcap,
                                   this_cycle.temp, dut.errormessage))
            time.sleep(INTERVAL)

    def check_dut_discharge(self):
        """ check auto/self discharge function on each DUT.
        :return: None
        """
        for dut in self.dut_list:
            if dut is None:
                continue
            config = load_test_item(self.config_list[dut.slotnum],
                                    "Self_Measured_Capacitor")
            if(not config["enable"]):
                continue
            if(config["stoponfail"]) & (dut.status != DUT_STATUS.Idle):
                continue

            # disable auto discharge
            self.switch_to_mb()
            self.auto_discharge(slot=dut.slotnum, status=False)
            # disable charge
            self.switch_to_dut(dut.slotnum)
            dut.charge(status=False)

            # enable self discharge
            dut.self_discharge(status=True)

        for i in range(SD_COUNTER):
            for dut in self.dut_list:
                if dut is None:
                    continue
                if(not config["enable"]):
                    continue
                if(config["stoponfail"]) & (dut.status != DUT_STATUS.Idle):
                    continue

                self.switch_to_dut(dut.slotnum)
                this_cycle = Cycle()
                this_cycle.vin = self.ps.measureVolt()
                try:
                    temperature = dut.check_temp()
                except aardvark.USBI2CAdapterException:
                    # temp ic not ready
                    temperature = 0
                this_cycle.temp = temperature
                this_cycle.counter = self.counter
                this_cycle.time = time.time()
                this_cycle.state = "self_discharge"
                self.ld.select_channel(dut.slotnum)
                this_cycle.vcap = self.ld.read_volt()
                self.counter += 1
                logger.info("dut: {0} status: {1} vcap: {2} "
                            "temp: {3} message: {4} ".
                            format(dut.slotnum, dut.status, this_cycle.vcap,
                                   this_cycle.temp, dut.errormessage))
                dut.cycles.append(this_cycle)
            time.sleep(INTERVAL)

        for dut in self.dut_list:
            if dut is None:
                continue
            config = load_test_item(self.config_list[dut.slotnum],
                                    "Self_Measured_Capacitor")
            if(not config["enable"]):
                continue
            if(config["stoponfail"]) & (dut.status != DUT_STATUS.Idle):
                continue
            if dut.status != DUT_STATUS.Idle:
                continue
            cap_list = []
            pre_vcap, pre_time = None, None
            for cycle in dut.cycles:
                if cycle.state == "self_discharge":
                    if pre_vcap is None:
                        pre_vcap = cycle.vcap
                        pre_time = cycle.time
                    else:
                        cur_vcap = cycle.vcap
                        cur_time = cycle.time
                        cap = (cur_time - pre_time)\
                            / (float(config["Resistance"]) * math.log(pre_vcap /
                                                                      cur_vcap))
                        cap_list.append(cap)
            if(len(cap_list) > 0):
                capacitor = sum(cap_list) / float(len(cap_list))
                dut.self_capacitance_measured = capacitor
                logger.debug(cap_list)
            else:
                dut.capacitance_measured = 0
            if not (config["min"] < dut.self_capacitance_measured <
                    config["max"]):
                dut.status = DUT_STATUS.Fail
                dut.errormessage = "Capacitor out of range."
                logger.info("dut: {0} self meas capacitor: {1} message: {2} ".
                            format(dut.slotnum, dut.capacitance_measured,
                                   dut.errormessage))

    def program_dut(self):
        """ program vpd of DUT.
        :return: None
        """
        for dut in self.dut_list:
            if dut is None:
                continue
            config = load_test_item(self.config_list[dut.slotnum],
                                    "Program_VPD")
            if(not config["enable"]):
                continue
            if(config["stoponfail"]) & (dut.status != DUT_STATUS.Idle):
                continue
            self.switch_to_dut(dut.slotnum)

            try:
                dut.write_vpd(config["File"])
                dut.read_vpd()
            except AssertionError:
                dut.status = DUT_STATUS.Fail
                dut.errormessage = "Programming VPD Fail"
                logger.info("dut: {0} status: {1} message: {2} ".
                            format(dut.slotnum, dut.status, dut.errormessage))

    def check_temperature_dut(self):
        """
        check temperature value of IC on DUT.
        :return: None.
        """
        for dut in self.dut_list:
            if dut is None:
                continue
            config = load_test_item(self.config_list[dut.slotnum],
                                    "Check_Temp")
            if(not config["enable"]):
                continue
            if(config["stoponfail"]) & (dut.status != DUT_STATUS.Idle):
                continue
            self.switch_to_dut(dut.slotnum)
            temp = dut.check_temp()
            if not (config["min"] < temp < config["max"]):
                dut.status = DUT_STATUS.Fail
                dut.errormessage = "Temperature out of range."
                logger.info("dut: {0} status: {1} message: {2} ".
                            format(dut.slotnum, dut.status, dut.errormessage))

    def check_encryptedic_dut(self):
        """ check the data in encrypted ic, if data is not all zero, dut is
        passed.
        :return: None
        """
        for dut in self.dut_list:
            if dut is None:
                continue
            config = load_test_item(self.config_list[dut.slotnum],
                                    "Check_EncryptedIC")
            if(not config["enable"]):
                continue
            if(config["stoponfail"]) & (dut.status != DUT_STATUS.Idle):
                continue
            if dut.status != DUT_STATUS.Idle:
                continue

            self.switch_to_dut(dut.slotnum)
            if(not dut.encrypted_ic()):
                dut.status = DUT_STATUS.Fail
                dut.errormessage = "Check I2C on Encrypted IC Fail."
                logger.info("dut: {0} status: {1} message: {2} ".
                            format(dut.slotnum, dut.status, dut.errormessage))

    def auto_discharge(self, slot, status=False):
        """output PRESENT/AUTO_DISCH signal on TCA9555 on mother board.
           When status=True, discharge;
           When status=False, not discharge.
        """
        if(status):
            IO = 0
        else:
            IO = 1

        chnum = self.channel

        self.adk.slave_addr = 0x20 + chnum
        REG_INPUT = 0x00
        REG_OUTPUT = 0x02
        REG_CONFIG = 0x06

        # config PIO-0 to output and PIO-1 to input
        # first PIO-0 then PIO-1
        wdata = [REG_CONFIG, 0x00, 0xFF]
        self.adk.write(wdata)

        # read current status
        val = self.adk.read_reg(REG_INPUT, length=2)
        val = val[0]    # only need port 0 value

        # set current slot
        if(IO == 1):
            # set bit
            val |= (IO << slot)
        else:
            # clear bit
            val &= ~(0X01 << slot)

        # output
        # first PIO-0, then PIO-1
        wdata = [REG_OUTPUT, val, 0xFF]
        self.adk.write(wdata)

        # read status back
        val = self.adk.read_reg(REG_INPUT, length=2)
        val = val[0]    # only need port 0 value
        val = (val & (0x01 << slot)) >> slot
        assert val == IO

    def switch_to_dut(self, slot):
        """switch I2C ports by PCA9548A, only 1 channel is enabled.
        chnum(channel number): 0~7
        slotnum(slot number): 0~7
        """
        chnum = self.channel
        self.adk.slave_addr = 0x70 + chnum    # 0111 0000
        wdata = [0x01 << slot]

        # Switch I2C connection to current PGEM
        # Need call this function every time before communicate with PGEM
        self.adk.write(wdata)

    def switch_to_mb(self):
        """switch I2C ports back to mother board
           chnum(channel number): 0~7
        """
        chnum = self.channel
        self.adk.slave_addr = 0x70 + chnum    # 0111 0000
        wdata = 0x00

        # Switch I2C connection to mother board
        # Need call this function every time before communicate with
        # mother board
        self.adk.write(wdata)

    def check_power_fail(self, status="OFF"):
        """check power_fail_int signal on TCA9555 on mother board
        """
        self.switch_to_mb()
        for dut in self.dut_list:
            if dut is None:
                continue
            config = load_test_item(self.config_list[dut.slotnum],
                                    "Check_PowerFailInt")
            if(not config["enable"]):
                continue
            if(config["stoponfail"]) & (dut.status != DUT_STATUS.Idle):
                continue
            if dut.status != DUT_STATUS.Idle:
                continue

            chnum = self.channel
            self.adk.slave_addr = 0x20 + chnum
            REG_INPUT = 0x00
            #REG_OUTPUT = 0x02
            REG_CONFIG = 0x06
            # config PIO-0 to output and PIO-1 to input
            # first PIO-0 then PIO-1
            wdata = [REG_CONFIG, 0x00, 0xFF]
            self.adk.write(wdata)
            # read reg_input
            val = self.adk.read_reg(REG_INPUT, length=2)
            val = val[1]    # only need port 1 value
            # check current slot
            val = (val & (0x01 << dut.slotnum)) >> dut.slotnum

            if(val != int(config[status])):
                dut.status = DUT_STATUS.Fail
                dut.errormessage = "check power_fail_int fail."
                logger.info("dut: {0} status: {1} int_io: {2} message: {3} ".
                            format(dut.slotnum, dut.status,
                                   val, dut.errormessage))

    def calculate_capacitance(self):
        """ calculate the capacitance of DUT, based on vcap list in discharging.
        :return: capacitor value
        """
        for dut in self.dut_list:
            if dut is None:
                continue
            config = load_test_item(self.config_list[dut.slotnum],
                                    "Capacitor")
            if(not config["enable"]):
                continue
            if(config["stoponfail"]) & (dut.status != DUT_STATUS.Idle):
                continue
            if dut.status != DUT_STATUS.Idle:
                continue
            cap_list = []
            pre_vcap, pre_time = None, None
            for cycle in dut.cycles:
                if cycle.state == "discharge":
                    if pre_vcap is None:
                        pre_vcap = cycle.vcap
                        pre_time = cycle.time
                    else:
                        cur_vcap = cycle.vcap
                        cur_time = cycle.time
                        cap = (self.current * (cur_time - pre_time))\
                              / (pre_vcap - cur_vcap)
                        cap_list.append(cap)
            if(len(cap_list) > 0):
                capacitor = sum(cap_list) / float(len(cap_list))
                dut.capacitance_measured = capacitor
                logger.debug(cap_list)
            else:
                dut.capacitance_measured = 0
            if not (config["min"] < dut.capacitance_measured < config["max"]):
                dut.status = DUT_STATUS.Fail
                dut.errormessage = "Capacitor out of range."
                logger.info("dut: {0} capacitor: {1} message: {2} ".
                            format(dut.slotnum, dut.capacitance_measured,
                                   dut.errormessage))

    def prepare_to_exit(self):
        """ cleanup and save to database before exit.
        :return: None
        """
        # setup database
        # db should be prepared in cli.py
        sm = SessionManager()
        sm.prepare_db("sqlite:///" + RESULT_DB, [DUT, Cycle])
        session = sm.get_session("sqlite:///" + RESULT_DB)

        for dut in self.dut_list:
            if dut is None:
                continue
            if(dut.status == DUT_STATUS.Idle):
                dut.status = DUT_STATUS.Pass
                msg = "passed"
            else:
                msg = dut.errormessage
            logger.info("TEST RESULT: dut {0} ===> {1}".format(
                dut.slotnum, msg))

            for pre_dut in session.query(DUT).\
                    filter(DUT.barcode == dut.barcode).all():
                pre_dut.archived = 1
                session.add(pre_dut)
                session.commit()
            dut.archived = 0
            session.add(dut)
            session.commit()
            time.sleep(2)

        session.close()

    def run(self):
        """ override thread.run()
        :return: None
        """
        while(not self.exit):
            state = self.queue.get()
            if(state == ChannelStates.EXIT):
                try:
                    self.prepare_to_exit()
                    self.exit = True
                    logger.info("Channel: Exit Successfully.")
                except Exception as e:
                    self.error(e)
            elif(state == ChannelStates.INIT):
                try:
                    logger.info("Channel: Initialize.")
                    self.init()
                    self.check_power_fail(status="OFF")
                    self.progressbar += 20
                except Exception as e:
                    self.error(e)
            elif(state == ChannelStates.CHARGE):
                try:
                    logger.info("Channel: Charge DUT.")
                    self.charge_dut()
                    self.progressbar += 30
                except Exception as e:
                    self.error(e)
            elif(state == ChannelStates.LOAD_DISCHARGE):
                try:
                    logger.info("Channel: Discharge DUT.")
                    self.discharge_dut()
                    self.progressbar += 20
                except Exception as e:
                    self.error(e)
            elif(state == ChannelStates.PROGRAM_VPD):
                try:
                    logger.info("Channel: Program VPD.")
                    self.check_power_fail(status="ON")
                    self.program_dut()
                    self.progressbar += 5
                except Exception as e:
                    self.error(e)
            elif(state == ChannelStates.CHECK_ENCRYPTED_IC):
                try:
                    logger.info("Channel: Check Encrypted IC.")
                    self.check_encryptedic_dut()
                    self.progressbar += 5
                except Exception as e:
                    self.error(e)
            elif(state == ChannelStates.CHECK_TEMP):
                try:
                    logger.info("Channel: Check Temperature")
                    self.check_temperature_dut()
                    self.progressbar += 5
                except Exception as e:
                    self.error(e)
            elif(state == ChannelStates.CHECK_CAPACITANCE):
                try:
                    logger.info("Channel: Check Capacitor Value")
                    self.calculate_capacitance()
                    self.progressbar += 5
                except Exception as e:
                    self.error(e)
            elif(state == ChannelStates.DUT_DISCHARGE):
                try:
                    logger.info("Channel: Self Mesaured Capacitor")
                    self.check_dut_discharge()
                    self.progressbar += 10
                except Exception as e:
                    self.error(e)
            else:
                logger.error("unknown dut state, exit...")
                self.exit = True

    def auto_test(self):
        self.queue.put(ChannelStates.INIT)
        self.queue.put(ChannelStates.CHARGE)
        self.queue.put(ChannelStates.PROGRAM_VPD)
        self.queue.put(ChannelStates.CHECK_ENCRYPTED_IC)
        self.queue.put(ChannelStates.CHECK_TEMP)
        self.queue.put(ChannelStates.DUT_DISCHARGE)
        self.queue.put(ChannelStates.LOAD_DISCHARGE)
        self.queue.put(ChannelStates.CHECK_CAPACITANCE)
        self.queue.put(ChannelStates.EXIT)
        self.start()

    def empty(self):
        for i in range(self.queue.qsize()):
            self.queue.get()

    def error(self, e):
        logger.error(e.message)
        self.exit = True
        raise e

    def quit(self):
        self.empty()
        self.queue.put(ChannelStates.EXIT)


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)

    barcode = ["AGIGA9603-004BCA02144800000002-06",
               "AGIGA9603-004BCA02144800000002-06",
               "AGIGA9603-004BCA02144800000002-06",
               "AGIGA9603-004BCA02144800000002-06"]
    ch = Channel(barcode_list=barcode, channel_id=0,
                 name="UFT_CHANNEL")
    #ch.start()
    #ch.queue.put(ChannelStates.INIT)
    #ch.queue.put(ChannelStates.CHARGE)
    #ch.queue.put(ChannelStates.PROGRAM_VPD)
    #ch.queue.put(ChannelStates.CHECK_ENCRYPTED_IC)
    #ch.queue.put(ChannelStates.CHECK_TEMP)
    #ch.queue.put(ChannelStates.LOAD_DISCHARGE)
    #ch.queue.put(ChannelStates.CHECK_CAPACITANCE)
    #ch.queue.put(ChannelStates.EXIT)
    ch.auto_test()
