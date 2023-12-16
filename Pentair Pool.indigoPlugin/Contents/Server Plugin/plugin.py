#! /usr/bin/env python
# -*- coding: utf-8 -*-
####################
# Pentair Pool Plugin for Indigo
# Developed by Jeremy Swancoat

import time
import serial
import requests
import xml.etree.ElementTree as ET

# Retry serial port interruptions:
kSerialRetry = 5


##################################################################################################
class Plugin(indigo.PluginBase):

    ##############################################################################################
    # Required Methods
    ##############################################################################################
    def __init__(self, pluginId, pluginDisplayName, pluginVersion, pluginPrefs):
        indigo.PluginBase.__init__(self, pluginId, pluginDisplayName, pluginVersion, pluginPrefs)
        if 'showDebugInfo' in pluginPrefs:
            self.debug = pluginPrefs['showDebugInfo']
        else:
            self.debug = False
        self.conn = None
        self.portEnabled = False
        self.commQueue = []
        self.circuit_dev = {}
        self.logTemps = False
        self.autelisIP = '0'
        self.system_names = {}
        self.modelList = (
            "i5p3", "i7p3", "i9p3", "i5p3S", "i9p3S", "i10p3D", "unknown", "unknown", "unknown", "unknown", "unknown", "unknown", "unknown",
            "unknown", "unknown", "EasyTouch 8")
        self.opMode = ("auto", "service")
        self.okErr = ("ok", "error")
        self.curTemp = 77
        self.next_refresh = time.time()
        self.refreshTemp = None
        self.refreshChem = None

        # Map of Pentair-style Circuit Codes and a quad-tuple indicating what device code the device will be found under,
        # (USUALLY, but not always, the same as the reported code)
        # what indigo variable state should be updated, a suffix for the log, and the type of processing the data requires.
        # Also, All 50 Auxiliary circuits are the same: AUXx = ("AUXx", "onOffState", "")
        self.pentairStateMap = {"POOLHT": ("POOLHT", "hvacOperationMode", " Mode", "hmode"),
                                "SPAHT": ("SPAHT", "hvacOperationMode", " Mode", "hmode"), "POOLSP": ("POOLHT", "setpointHeat", " Set Point", "temp"),
                                "SPASP": ("SPAHT", "setpointHeat", " Set Point", "temp"), "AIRTMP": ("SYSTEM", "airtemp", " Air Temperature", "temp"),
                                "POOLTMP": ("POOLHT", "temperatureInput1", " Temperature", "temp"),
                                "SPATMP": ("SPAHT", "temperatureInput1", " Temperature", "temp"),
                                "OPMODE": ("SYSTEM", "opmode", " Operation Mode", "ilinkOpmode")}

        # Map of Autelis' variable names to the states used in the 'system' device type.
        self.autelisStateMap = {"runstate": "readystate", "model": "model", "opmode": "opmode", "freeze": "freeze", "sensor1": "water_sensor",
                                "sensor2": "solar_sensor", "sensor3": "air_sensor", "sensor4": "water_sensor_2", "sensor5": "solar_sensor_2",
                                "version": "version", "airtemp": "airtemp", "soltemp": "solartemp", "poolsp": "pool_chlor", "spasp": "spa_chlor",
                                "salt": "salt", "super": "super_chlor", "chlorname": "chlorname", "chlorerr": "chlorerr"}

        # similar to self.autelisStateMap, but since info from the temp node in status.xml may refer to pool heater, spa heater or system,
        # the values in this dictionary are a tuple indicating the device (by circuit code) and the variable name
        self.autelisTempMap = {"poolht": ("POOLHT", "hvacOperationMode"), "spaht": ("SPAHT", "hvacOperationMode"),
                               "htstatus": ("POOLHT", "hvacHeaterIsOn"), "poolsp": ("POOLHT", "setpointHeat"), "spasp": ("SPAHT", "setpointHeat"),
                               "pooltemp": ("POOLHT", "temperatureInput1"), "spatemp": ("SPAHT", "temperatureInput1"),
                               "airtemp": ("SYSTEM", "airtemp"), "soltemp": ("SYSTEM", "solartemp")}

    ##############################################################################################
    # Non-Required Methods (but still defined by Indigo)
    ##############################################################################################
    def startup(self):
        # startup process for the serial device. Other than open the serial port, there should be none really. It just runs.
        self.logger.debug("Startup Called")

        if self.conn is None:
            pass
        else:
            self.conn.close()
        self.portEnabled = False

        serialUrl = self.getSerialPortUrl(self.pluginPrefs, "serialport")
        if 'interface' in self.pluginPrefs:
            if self.pluginPrefs['interface'] == 'autelis':
                self.autelisIP = serialUrl[9:].split(":")[0]
                self.logger.debug(f"Autelis IP Address: {self.autelisIP}")
        if serialUrl == "":
            pass
        else:
            self.logger.info(f"Serial Port URL is{serialUrl}")
            self.conn = self.openSerial("Pentair Intellitouch", serialUrl, 9600, stopbits=1, timeout=0.5, writeTimeout=1)

        if self.conn is not None:
            self.portEnabled = True
            self.logger.info(f"Serial Port Open at {serialUrl}")

    def shutdown(self):
        # close serial port here
        self.logger.debug("Shutdown Called")
        self.conn.close()
        self.logger.info("Serial Port Closed")

    def validateDeviceConfigUi(self, valuesDict, typeId, devId):
        # Validate that appropriate selections were made in device creation / modification dialogs.
        # Ensure that Zone is not already used
        errorsDict = indigo.Dict()
        circuit = valuesDict["circuitselect"]
        self.logger.debug(f"Circuit Selected: {circuit}")
        self.logger.debug("Validating Device")
        if circuit in self.circuit_dev:
            errorsDict["circuitselect"] = "Circuit Already Assigned to Device"
        else:
            self.logger.debug("Circuit Available")

        if len(errorsDict) > 0:
            return False, valuesDict, errorsDict
        return True, valuesDict

    # This section creates entries and deletes entries in a dictionary of
    # AUXx , dev in an effort help find devices simply by what zone they control.
    def deviceStartComm(self, dev):
        self.logger.debug("deviceStartComm called")
        circuitcode = dev.pluginProps["circuitselect"]
        self.circuit_dev[dev.pluginProps["circuitselect"]] = dev
        self.logger.debug("Just added: " + circuitcode + " to circuitdev")
        if dev.deviceTypeId == "circuit":
            self.commQueue.append(circuitcode + " ?")
        elif dev.deviceTypeId == "heater":
            # If we're starting pool or spa heater devices, we'll need more status info... setpoint and temp.
            if self.pluginPrefs['interface'] == "autelis":
                self.autelisProcessTemp()
            else:
                self.commQueue.append(circuitcode + " ?")
                self.commQueue.append(circuitcode[:-2] + "SP ?")
                self.commQueue.append(circuitcode[:-2] + "TMP ?")
        elif dev.deviceTypeId == "system":
            if self.pluginPrefs['interface'] == "autelis":
                self.autelisProcessStatus()
                self.autelisProcessChem()

    def deviceStopComm(self, dev):
        del self.circuit_dev[dev.pluginProps["circuitselect"]]
        self.logger.debug("Just deleted: " + dev.pluginProps["circuitselect"] + " from circuitdev")

    def validatePrefsConfigUi(self, valuesDict):

        errorsDict = indigo.Dict()
        self.logger.debug("Validating Serial Port")
        self.validateSerialPortUi(valuesDict, errorsDict, "serialport")

        if valuesDict['showDebugInfo']:
            self.debug = True
        else:
            self.debug = False

        if valuesDict['logTemps']:
            self.logTemps = True
        else:
            self.logTemps = False

        if len(errorsDict) > 0:
            return False, valuesDict, errorsDict
        return True, valuesDict

    def closedPrefsConfigUi(self, valuesDict, userCancelled):
        # Basically, when the Config box closes, we want another chance to open the serial port.
        # Since that's all the startup method does, just call it from here.
        self.logger.debug("closedPrefsConfigUI() called")

        # Obviously, if the box closes by cancelling, then do nothing.
        if userCancelled:
            pass
        else:
            self.startup()

    def runConcurrentThread(self):
        # This is what runs as the plugin is simply 'running'.
        # Basically, if a command comes in from the serial device, you read it, parse it and hand it off to
        # whatever device/or variable needs to have its state updated.
        # In the meantime, you have a queue set up which gets loaded with commands from devices which must be fed
        # to the serial interface to be enacted.
        sleeptime = 0.1
        try:
            while True:
                if self.portEnabled:
                    from_pentair = self.conn.readline().decode('ascii')
                    if len(from_pentair) > 3:
                        self.logger.debug(f"From Pentair: {from_pentair[:-1]}")  # removes last character (CR)
                        self.parseToServer(from_pentair[:-1])
                    if len(self.commQueue) > 0:
                        command = self.commQueue.pop(0)
                        if self.pluginPrefs['interface'] == 'autelis':
                            command = command.replace(" ", "")
                        else:
                            command = command.replace("POOL ", "PUMP ")
                        self.logger.debug("To Pentair: " + command)
                        sendcount = self.conn.write("#" + command + "\r")
                if self.pluginPrefs['interface'] == 'autelis':
                    if time.time() > self.next_refresh:
                        self.logger.debug("Refreshing Panel Status...")
                        self.autelisProcessStatus()
                    if time.time() > self.refreshChem:
                        self.logger.debug("Refreshing Chem...")
                        self.autelisProcessChem()
                        self.autelisProcessTemp()

                self.sleep(sleeptime)
        except self.StopThread:
            pass
        except serial.SerialException:
            self.conn.close()
            self.sleep(10)
            self.logger.error("Serial Connection Lost. Trying again in 10 seconds...")
            self.startup()
        except Exception as e:
            self.logger.error(f"Error in runConcurrentThread: {e}")

    def parseToServer(self, frompi):
        # Looks Messy, but it's really just a bunch of tests to see what kind of statement it is
        # and then handling it - start with simple statements, so we can just move on from there.
        # Start by checking for errors...
        if frompi[0] == "?":
            self.errorLog("Pentair Error: " + frompi[1:])
        elif frompi[:3] == "!00":
            # Now that we know this is giving status of a circuit, we can decide
            # which circuit and update the state on server. If not defined, just bail out and discard the response.
            # Now, just move on to parsing out which circuit does what.
            #
            #
            # Responses from Pentair iLink and Autellis adapter vary slightly on spacing, so reformat the response to match
            if self.pluginPrefs['interface'] == 'autelis':
                frompi = frompi.replace("=", " = ")
            frompi = frompi.replace("PUMP", "POOL")
            resplist = frompi.split()
            responseCode = resplist[1]
            # Different reported circuits all map to different devices, variable names and have different data formats
            # Basically, look up processing instructions from a dictionary, and process.
            if responseCode in self.pentairStateMap:
                circuitcode = self.pentairStateMap[responseCode][0]
                statecode = self.pentairStateMap[responseCode][1]
                suffix = self.pentairStateMap[responseCode][2]
                dataproc = self.pentairStateMap[responseCode][3]
            else:
                circuitcode = responseCode
                statecode = "onOffState"
                suffix = ""
                dataproc = "onOff"

            if dataproc == "hmode":
                if self.pluginPrefs['interface'] == 'autelis':
                    modes = {"HEATER": 1, "OFF": 0}
                    repvalue = modes[resplist[3]]
                else:
                    repvalue = int(resplist[3])
            elif dataproc == "temp":
                repvalue = int(resplist[3])
            elif dataproc == "ilinkOpmode":
                repvalue = self.ilinkOpmode(resplist[3])
            else:
                modes = ("off", "on")
                repvalue = modes[int(resplist[3])]

            # With all data now correctly formatted, just check if the circuit's in use, update and log the changes.
            if circuitcode in self.circuit_dev:
                servdev = self.circuit_dev[circuitcode]
                servdev.updateStateOnServer(statecode, value=repvalue)
                if 'temp' in statecode:
                    if self.logTemps:
                        self.logger.info(servdev.name + suffix + " is " + str(repvalue))
                else:
                    self.logger.info(servdev.name + suffix + " is " + str(repvalue))

            else:
                self.logger.debug("Circuit " + circuitcode + " currently not in use by Indigo.")

    def actionControlDimmerRelay(self, action, dev):
        circuitcode = dev.pluginProps["circuitselect"]
        comOn = " = 1"
        comOff = " = 0"
        if self.pluginPrefs['interface'] == 'autelis':
            comOn = "=T"
            comOff = "=F"
        self.logger.debug("actionControlDimmerRelay called")
        self.logger.debug("circuitcode is: " + circuitcode)
        if action.deviceAction == indigo.kDeviceAction.TurnOn:
            self.logger.info("Turn On " + dev.name)
            self.logger.debug("Turn On " + circuitcode)
            self.commQueue.append(circuitcode + comOn)
        elif action.deviceAction == indigo.kDeviceAction.TurnOff:
            self.logger.info("Turn Off " + dev.name)
            self.logger.debug("Turn Off " + circuitcode)
            self.commQueue.append(circuitcode + comOff)
        elif action.deviceAction == indigo.kDeviceAction.Toggle:
            self.logger.info("Toggle " + dev.name)
            self.logger.debug("Toggle " + circuitcode)
            self.logger.debug("Device onState: " + str(dev.onState))
            if dev.onState == 0:
                self.commQueue.append(circuitcode + comOn)
            else:
                self.commQueue.append(circuitcode + comOff)
        if circuitcode == "POOL":
            self.commQueue.append("SPA ?")
        elif circuitcode == "SPA":
            self.commQueue.append("POOL ?")

    def actionControlThermostat(self, action, dev):
        circuitcode = dev.pluginProps["circuitselect"]
        self.logger.debug("circuitcode is: " + circuitcode)
        # insert appropriate commands to issue to Pentair system for each possible thermostat command.
        if action.thermostatAction == indigo.kThermostatAction.SetHvacMode:
            if self.pluginPrefs["interface"] == 'autelis':
                if action.actionMode == 0:
                    sendMode = "OFF"
                elif action.actionMode == 1:
                    sendMode = "HEATER"
                else:
                    sendMode = "OFF"
            else:
                sendMode = str(action.actionMode)
            self.commQueue.append(circuitcode + " = " + sendMode)
            pass
        elif action.thermostatAction == indigo.kThermostatAction.SetHeatSetpoint:
            newSetpoint = action.actionValue
            self.commQueue.append(circuitcode[:-2] + "SP = " + str(int(action.actionValue)))
        elif action.thermostatAction == indigo.kThermostatAction.IncreaseHeatSetpoint:
            newSetpoint = dev.heatSetpoint + action.actionValue
            self.commQueue.append(circuitcode[:-2] + "SP = " + str(int(newSetpoint)))
        elif action.thermostatAction == indigo.kThermostatAction.DecreaseHeatSetpoint:
            newSetpoint = dev.heatSetpoint - action.actionValue
            self.commQueue.append(circuitcode[:-2] + "SP = " + str(int(newSetpoint)))
        elif action.thermostatAction in [indigo.kThermostatAction.RequestStatusAll, indigo.kThermostatAction.RequestMode,
                                         indigo.kThermostatAction.RequestEquipmentState, indigo.kThermostatAction.RequestTemperatures,
                                         indigo.kThermostatAction.RequestSetpoints]:
            if self.pluginPrefs["interface"] == 'autelis':
                self.autelisProcessTemp()
            else:
                self.commQueue.append(circuitcode + " ?")
                self.commQueue.append(circuitcode[:-2] + "SP ?")
                self.commQueue.append(circuitcode[:-2] + "TMP ?")
            pass

    def intellibriteOn(self, pluginAction):
        if self.pluginPrefs['interface'] == 'autelis':
            self.autelisCommand("lights", "none", "val", "allon")
        else:
            self.commQueue.append("ALLLIGHTS = 1")

    def intellibriteOff(self, pluginAction):
        if self.pluginPrefs['interface'] == 'autelis':
            self.autelisCommand("lights", "none", "val", "alloff")
        else:
            self.commQueue.append("ALLLIGHTS = 0")

    def setSetPoint(self, pluginAction, dev):
        self.logger.info("Change Set Point of " + dev.name + " to " + str(pluginAction.props.get(u"reqtemp")))
        circuitbase = dev.pluginProps["circuitselect"]
        operator = ""
        if circuitbase == "POOL":
            operator = "POOLSP"
        elif circuitbase == "SPA":
            operator = "SPASP"
        self.commQueue.append(operator + " = " + str(pluginAction.props.get(u"reqtemp")))

    def setIntellibriteMode(self, pluginAction):
        setmode = pluginAction.props.get(u"newmode")
        self.logger.info("Setting Intellibrite Mode to " + setmode)
        if self.pluginPrefs['interface'] == 'autelis':
            setmode = setmode[3:].lower()
            self.autelisCommand("lights", "none", "val", setmode)
        else:
            self.commQueue.append(setmode)

    def superChlor(self, pluginAction):
        hours = min(int(pluginAction.props.get("hours")), 24)
        if self.pluginPrefs['interface'] == 'autelis':
            self.logger.info("Super-Chlorinating for " + str(hours) + " hours")
            self.autelisCommand("chlor", "SYSTEM", "super", str(hours))
        else:
            self.logger.error("Interface must be 'Autelis' to execute Super-Chlor Action")

    def adjChlor(self, pluginAction):
        param = pluginAction.props.get("circuit")
        level = max(min(int(pluginAction.props.get("chlorLevel")), 100), 0)
        if self.pluginPrefs['interface'] == 'autelis':
            self.logger.info("Setting " + param + " chlorination level to " + str(level) + "%")
            self.autelisCommand("chlor", "SYSTEM", param, str(level))
        else:
            self.logger.error("Interface must be 'Autelis' to set chlorination level")

    def genAuxCircuitList(self, filter, valuesDict, typeID, targetID):
        # method to generate lists with names/labels.
        auxList = []
        for cir in range(1, 51):
            pname = "AUX" + str(cir)
            auxList.append((pname, pname))
        auxList.append(("POOL", "POOL"))
        auxList.append(("SPA", "SPA"))
        self.logger.debug(f"genAuxCircuitList: {auxList}")
        return auxList

    @staticmethod
    def autelisRunstate(rvalue):
        rvalue = int(rvalue)
        if rvalue == 1:
            rstate = "startingUp"
        elif rvalue == 50:
            rstate = "ready"
        else:
            rstate = "gettingData"

        return rstate

    def autelisCommand(self, comGroup, circuit, param, value):
        url = "http://" + self.autelisIP + "/" + comGroup + ".cgi"
        if comGroup == "set":
            payload = {'name': circuit, param: value}
        else:
            payload = {param: value}
        req = requests.get(url, params=payload, auth=('admin', self.pluginPrefs['autpwd']))
        response = req.text
        self.logger.debug("Command sent to Autellis. Response: " + response)

    def autelisProcessNames(self):
        # get the names.xml file from the Autelis Interface and assign to inputs
        self.logger.debug("Loading names file from Autelis Interface...")
        xmlset = "names"
        url = "http://" + self.autelisIP + "/" + xmlset + ".xml"
        req = requests.get(url, auth=('admin', self.pluginPrefs['autpwd']))
        autdata = ET.XML(req.text)
        for child in autdata.find('equipment'):
            self.system_names[child.tag] = child.text

    def autelisProcessStatus(self):
        # get the status.xml file from the Autelis Interface and parse system node to devices
        self.logger.debug("Processing system node from Autelis Status.xml...")
        xmlset = "status"
        url = "http://" + self.autelisIP + "/" + xmlset + ".xml"
        req = requests.get(url, auth=('admin', self.pluginPrefs['autpwd']))
        autdata = ET.XML(req.text)
        if "SYSTEM" in self.circuit_dev:
            self.logger.debug("'System' Device Available")
            dev = self.circuit_dev["SYSTEM"]
            for child in autdata.find('system'):
                if child.tag in self.autelisStateMap:
                    statecode = self.autelisStateMap[child.tag]
                    repvalue = child.text
                    if statecode == "readystate":
                        repvalue = self.autelisRunstate(repvalue)
                    elif statecode == "model":
                        repvalue = self.modelList[int(repvalue)]
                    elif statecode == "opmode":
                        repvalue = self.opMode[int(repvalue)]
                    elif child.tag[:6] == "sensor":
                        repvalue = self.okErr[int(repvalue)]
                    curstate = dev.states[statecode]
                    if curstate == repvalue:
                        self.logger.debug(dev.name + ": " + statecode + " is " + repvalue)
                    else:
                        dev.updateStateOnServer(statecode, value=repvalue)
                        self.logger.info(dev.name + ": " + statecode + " is " + repvalue)
        else:
            self.logger.debug("No 'System' Device Defined")
        self.next_refresh = time.time() + (60 * float(self.pluginPrefs['statusPoll']))

    def autelisProcessTemp(self):
        # get the status.xml file from the Autelis Interface and parse temp node to devices
        self.logger.debug("Processing temp node from Autelis Status.xml...")
        xmlset = "status"
        url = "http://" + self.autelisIP + "/" + xmlset + ".xml"
        req = requests.get(url, auth=('admin', self.pluginPrefs['autpwd']))
        autdata = ET.XML(req.text)
        for child in autdata.find('temp'):
            if child.text is None:
                pass
            else:
                if child.tag in self.autelisTempMap:
                    circuitcode = self.autelisTempMap[child.tag][0]
                    statecode = self.autelisTempMap[child.tag][1]
                    repvalue = int(child.text)
                    if child.tag == 'pooltemp':
                        self.curTemp = repvalue
                    if child.tag == 'htstatus':
                        # the heater status item carries information for multiple device, so it has to
                        # be pulled out and handled on its own.
                        stateDict = self.decodeHeatStatus(child.text)
                        for circuit in stateDict:
                            if circuit in self.circuit_dev:
                                dev = self.circuit_dev[circuit]
                                repvalue = stateDict[circuit]
                                dev.updateStateOnServer(statecode, value=repvalue)
                    elif circuitcode in self.circuit_dev:
                        dev = self.circuit_dev[circuitcode]
                        curstate = dev.states[statecode]
                        if curstate == repvalue:
                            self.logger.debug(dev.name + ": " + statecode + " is " + str(repvalue))
                        else:
                            dev.updateStateOnServer(statecode, value=repvalue)
                            self.logger.info(dev.name + ": " + statecode + " is " + str(repvalue))

    def autelisProcessChem(self):
        # get the status.xml file from the Autelis Interface and parse system node to devices
        self.logger.debug("Processing chlor node from Autelis Chem.xml...")
        xmlset = "chem"
        url = "http://" + self.autelisIP + "/" + xmlset + ".xml"
        req = requests.get(url, auth=('admin', self.pluginPrefs['autpwd']))
        autdata = ET.XML(req.text)
        if "SYSTEM" in self.circuit_dev:
            self.logger.debug("'System' Device Available")
            dev = self.circuit_dev["SYSTEM"]
            for child in autdata.find('chlor'):
                self.logger.debug("Child: " + str(child.tag))
                if child.tag in self.autelisStateMap:
                    self.logger.debug(str(child.tag) + " found in autelisStateMap")
                    statecode = self.autelisStateMap[child.tag]
                    repvalue = child.text
                    if statecode == "salt":
                        repvalue = 50 * int(repvalue)
                        adjsalt = repvalue - ((self.curTemp - 77) * 40)
                        if dev.states["temp_corr_salt"] == adjsalt:
                            self.logger.debug(dev.name + ": Temp Corrected Salt Level is " + str(adjsalt))
                        else:
                            dev.updateStateOnServer("temp_corr_salt", value=adjsalt)
                            self.logger.info(dev.name + ": Temp Corrected Salt Level is " + str(adjsalt))
                    elif statecode == "chlorname":
                        repvalue = child.text
                    elif statecode == "chlorerr":
                        repvalue = self.decodeChlorErr(child.text)
                    else:
                        repvalue = int(repvalue)
                    curstate = dev.states[statecode]
                    if curstate == repvalue:
                        self.logger.debug(dev.name + ": " + statecode + " is " + str(repvalue))
                    else:
                        dev.updateStateOnServer(statecode, value=repvalue)
                        self.logger.info(dev.name + ": " + statecode + " is " + str(repvalue))
        else:
            self.logger.debug("No 'System' Device Defined")
        # Save a timestamp for when this was last run, so that it can periodically be re-run.
        self.refreshChem = round(time.time(), 0) + (60 * float(self.pluginPrefs['chemPoll']))

    def decodeHeatStatus(self, value):
        state = (False, True)
        circuitcodes = ("POOLHT", "SPAHT", "SOLHT", "SPASOLHT")
        converted = bin(int(value))[2:]
        padded = ((4 - len(converted)) * '0' + converted)[::-1]
        rstates = {}
        for bit in range(0, 4):
            rstates[circuitcodes[bit]] = state[int(padded[bit])]
            self.logger.debug(circuitcodes[bit] + ": " + str(rstates[circuitcodes[bit]]))

        return rstates

    def decodeChlorErr(self, value):
        errors = ("Check Flow/PCB", "Low Salt", "Very Low Salt", "High Current", "Clean Cell", "Low Voltage", "Water Temp Low", "No Comm")
        converted = bin(int(value))[2:]
        padded = ((8 - len(converted)) * '0' + converted)[::-1]
        state = ""
        for bit in range(0, 8):
            if padded[bit] == '1':
                state = state + ", " + errors[int(padded[bit])]
        if state == "":
            state = "No errors"
        else:
            state = state[2:]
        self.logger.debug("Chlorinator Errors: " + state)

        return state

    @staticmethod
    def ilinkOpmode(value):
        if value == "AUTO":
            rval = "auto"
        else:
            rval = "service"

        return rval
