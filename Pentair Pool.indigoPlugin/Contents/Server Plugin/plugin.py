#! /usr/bin/env python
# -*- coding: utf-8 -*-
####################
# Pentair Pool Plugin for Indigo
# Developed by Jeremy Swancoat
# Updated for Indigo 2022 (Python 3) by Joe Keenan

import os
import sys
import time
import serial


# Note the "indigo" module is automatically imported and made available inside
# our global name space by the host process.

##################################################################################################
# 1 sec seems like a balanced sleep after commands between the USB-to-Serial and Aqualink Serial RS connection.  It's slooooow.

##################################################################################################
class Plugin(indigo.PluginBase):

    ##############################################################################################
    # Required Methods
    ##############################################################################################
    ##############################################################################################
    def __init__(self, pluginId, pluginDisplayName, pluginVersion, pluginPrefs):
        indigo.PluginBase.__init__(self, pluginId, pluginDisplayName, pluginVersion, pluginPrefs)
        # Setting debug to True will show you verbose debugging information in the Indigo Event Log
        # LOTS of debugLog commands are placed throughout this routine.
        self.debug = None
        self.conn = None
        self.portEnabled = False
        self.commQueue = []
        self.circuitdev = {}
        self.airtempvar = 0
        self.logTemps = False
        self.airtemptvar = None

    def startup(self):
        # startup process for the serial device. Other than open the serial port, there should be none really. It just runs.
        self.debugLog("Startup Called")

        if self.conn is None:
            pass
        else:
            self.conn.close()

        self.portEnabled = False
        serialUrl = self.getSerialPortUrl(self.pluginPrefs, "serialport")
        self.debugLog(f"Serial Port URL is {serialUrl}")
        self.conn = self.openSerial(u"Pentair Intellitouch", serialUrl, 9600, stopbits=1, timeout=0.5, writeTimeout=1)
        if self.conn is not None:
            self.portEnabled = True
            indigo.server.log(f"Serial Port Open at {serialUrl}")
            self.commQueue.append("RSPFMT = 0")
            self.commQueue.append("COSMSGS = 1")
            # Create a variable to store the air temp in, but check if it's already been created first, and if so,
            # just give it a local reference.
            try:
                self.airtempvar = indigo.variables["Pentair_Air_Temp"]
            except (Exception,):
                self.airtemptvar = indigo.variable.create("Pentair_Air_Temp")

            self.commQueue.append("AIRTMP ?")

    def shutdown(self):
        self.debugLog("Shutdown Called")
        self.conn.close()
        indigo.server.log("Serial Port Closed")

    def validateDeviceConfigUi(self, valuesDict, typeId, devId):
        # Validate that appropriate selections were made in device creation / modification dialogs.
        # Ensure that Zone is not already used
        self.debugLog("Validating Device")
        self.debugLog(f"Circuit Selected: {valuesDict['circuitselect']}")
        # Logic to see if device is in use
        if valuesDict["circuitselect"] in self.circuitdev:
            self.debugLog("Circuit Already Assigned to Device")
            return False, valuesDict
        # Need to add logic to test if the device is already used.
        self.debugLog("Circuit Available")
        return True, valuesDict

    # This section creates entries and deletes entries in a dictionary of
    # AUXx , dev in an effort help find devices simply by what zone they control.
    def deviceStartComm(self, dev):
        self.debugLog("deviceStartComm called")
        circuitcode = dev.pluginProps["circuitselect"]
        self.circuitdev[dev.pluginProps["circuitselect"]] = dev
        self.debugLog(f"Just added: {circuitcode} to circuitdev")
        # Call for device status
        self.commQueue.append(circuitcode + " ?")
        # If we're starting pool or spa, we'll need more status info... setpoint and temp.
        if circuitcode == "PUMP":
            self.commQueue.append("POOLSP ?")
            self.commQueue.append("POOLTMP ?")
        if circuitcode == "SPA":
            self.commQueue.append("SPASP ?")
            self.commQueue.append("SPATMP ?")

    def deviceStopComm(self, dev):
        del self.circuitdev[dev.pluginProps["circuitselect"]]
        self.debugLog(f"Just deleted: {dev.pluginProps['circuitselect']} from circuitdev")

    def validatePrefsConfigUi(self, valuesDict):

        errorsDict = indigo.Dict()
        self.debugLog("Validating Serial Port")
        self.validateSerialPortUi(valuesDict, errorsDict, u"serialport")

        if valuesDict['showDebugInfo']:
            self.debug = True
        else:
            self.debug = False
        # NEW SECTION
        if valuesDict[u'logTemps']:
            self.logTemps = True
        else:
            self.logTemps = False
        # END NEW SECTION

        if len(errorsDict) > 0:
            return False, valuesDict, errorsDict
        return True, valuesDict

    def closedPrefsConfigUi(self, valuesDict, userCancelled):
        # Basically, when the Config box closes, we want another chance to open the serial port.
        # Since that's all the startup method does, just call it from here.
        self.debugLog("closedPrefsConfigUI() called")

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
        try:
            while True:
                if self.portEnabled:
                    frompentair = 0
                    frompentair = self.conn.readline()
                    if len(frompentair) > 3:
                        self.debugLog(f"Pentair: {frompentair[:-1]}")  # removes last character (CR)
                        self.parseToServer(frompentair[:-1])
                    if len(self.commQueue) > 0:
                        self.debugLog(self.commQueue[0])
                        sendcount = self.conn.write("#" + self.commQueue.pop(0) + "\r")
                self.sleep(0.1)
        except self.StopThread:
            pass

    def parseToServer(self, frompi):
        # Looks Messy, but it's really just a bunch of tests to see what kind of statement it is
        # and then handling it - start with simple statements, so we can just move on from there.
        # Start by checking for errors...
        if frompi[0] == "?":
            self.errorLog(f"Pentair Error: {frompi[1:]}")
        elif frompi[:3] == "!00":
            # Now that we know this is giving status of a circuit, we can decide
            # which circuit and update the state on server. If not defined, just bail out and discard the response.
            # Now, just move on to parsing out which circuit does what.
            resplist = frompi.split()
            circuitcode = resplist[1]
            statecode = "onOffState"
            suffix = ""
            if circuitcode == "POOLSP":
                circuitcode = "PUMP"
                statecode = "SetPoint"
                suffix = " Set Point"
            elif circuitcode == "SPASP":
                circuitcode = "SPA"
                statecode = "SetPoint"
                suffix = " Set Point"
            elif circuitcode == "POOLTMP":
                circuitcode = "PUMP"
                statecode = "Temperature"
                suffix = " Temperature"
            elif circuitcode == "SPATMP":
                circuitcode = "SPA"
                statecode = "Temperature"
                suffix = " Temperature"
            if circuitcode in self.circuitdev:
                servdev = self.circuitdev[circuitcode]
                if resplist[3] == "0":
                    logstate = "off"
                    servdev.updateStateOnServer(statecode, value="off")
                elif resplist[3] == "1":
                    logstate = "on"
                    servdev.updateStateOnServer(statecode, value="on")
                else:
                    logstate = str(resplist[3])
                    servdev.updateStateOnServer(statecode, value=resplist[3])
                if (circuitcode == "PUMP" or circuitcode == "SPA") and statecode == "Temperature":
                    if self.logTemps:
                        indigo.server.log(servdev.name + suffix + " is " + logstate)
                else:
                    indigo.server.log(servdev.name + suffix + " is " + logstate)

            elif circuitcode == "AIRTMP":
                indigo.variable.updateValue(self.airtempvar, resplist[3])
                self.airtempvar.refreshFromServer()
                if self.logTemps:
                    indigo.server.log(f"Pool Air Temp is {resplist[3]}")
                    self.debugLog("Updating Air Temp variable.")
            else:
                self.debugLog(f"Circuit {circuitcode} currently not in use by Indigo.")

    def actionControlDimmerRelay(self, action, dev):
        circuitcode = dev.pluginProps["circuitselect"]
        self.debugLog("actionControlDimmerRelay called")
        self.debugLog(f"circuitcode is: {circuitcode}")
        if action.deviceAction == indigo.kDeviceAction.TurnOn:
            indigo.server.log("Turn On " + dev.name)
            self.debugLog(f"Turn On {circuitcode}")
            self.commQueue.append(circuitcode + " = 1")
        elif action.deviceAction == indigo.kDeviceAction.TurnOff:
            indigo.server.log(f"Turn Off {dev.name}")
            self.debugLog("Turn Off " + circuitcode)
            self.commQueue.append(circuitcode + " = 0")
        elif action.deviceAction == indigo.kDeviceAction.Toggle:
            indigo.server.log(f"Toggle {dev.name}")
            self.debugLog(f"Toggle {circuitcode}")
            self.debugLog(f"Device onState: {dev.onState}")
            if dev.onState == 0:
                self.commQueue.append(circuitcode + " = 1")
            else:
                self.commQueue.append(circuitcode + " = 0")
        if circuitcode == "PUMP":
            self.commQueue.append("SPA ?")
        elif circuitcode == "SPA":
            self.commQueue.append("PUMP ?")

    def intellibriteOn(self, pluginAction):
        self.commQueue.append("ALLLIGHTS = 1")

    def intellibriteOff(self, pluginAction):
        self.commQueue.append("ALLLIGHTS = 0")

    def setSetPoint(self, pluginAction, dev):
        indigo.server.log(f"Change Set Point of {dev.name} to {pluginAction.props.get('reqtemp')}")
        circuitbase = dev.pluginProps["circuitselect"]
        operator = ""
        if circuitbase == "PUMP":
            operator = "POOLSP"
        elif circuitbase == "SPA":
            operator = "SPASP"
        self.commQueue.append(operator + " = " + str(pluginAction.props.get(u"reqtemp")))

    def setIntellibriteMode(self, pluginAction):
        setmode = pluginAction.props.get(u"newmode")
        indigo.server.log(f"Setting Intellibrite Mode to {setmode}")
        self.commQueue.append(setmode)
