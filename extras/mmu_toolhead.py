# Happy Hare MMU Software
# Implementation of "MMU Toolhead" to allow for:
#   - "drip" homing and movement without pauses
#   - bi-directional syncing of extruder to gear rail or gear rail to extruder
#   - extruder endstops
#
# Copyright (C) 2023  moggieuk#6538 (discord)
#                     moggieuk@hotmail.com
#
# Based heavily on code by Kevin O'Connor <kevin@koconnor.net>
#
# (\_/)
# ( *,*)
# (")_(") MMU Ready
#
# This file may be distributed under the terms of the GNU GPLv3 license.
#
import logging, importlib, math, os, time
import stepper, chelper, toolhead
from extras.homing import Homing, HomingMove
from kinematics.extruder import PrinterExtruder, DummyExtruder

SDS_CHECK_TIME = 0.001 # step+dir+step filter in stepcompress.c

# Main code to track events (and their timing) on the MMU Machine implemented as additional "toolhead"
# (code pulled from toolhead.py)
class MmuToolHead(toolhead.ToolHead, object):
    def __init__(self, config):
        self.printer = config.get_printer()
        self.reactor = self.printer.get_reactor()
        self.all_mcus = [m for n, m in self.printer.lookup_objects(module='mcu')]
        self.mcu = self.all_mcus[0]
        self.can_pause = True
        if self.mcu.is_fileoutput():
            self.can_pause = False
        self.move_queue = toolhead.MoveQueue(self) # Use base class MoveQueue
        self.gear_motion_queue = self.extruder_synced_to_gear = None # For bi-directional syncing of gear and extruder
        self.commanded_pos = [0., 0., 0., 0.]
        self.printer.register_event_handler("klippy:shutdown", self._handle_shutdown)

        # Velocity and acceleration control
        self.gear_max_velocity = config.getfloat('gear_max_velocity', 300, above=0.)
        self.gear_max_accel = config.getfloat('gear_max_accel', 500, above=0.)
        self.selector_max_velocity = config.getfloat('selector_max_velocity', 250, above=0.)
        self.selector_max_accel = config.getfloat('selector_max_accel', 1500, above=0.)

        self.max_velocity = max(self.selector_max_velocity, self.gear_max_velocity)
        self.max_accel = max(self.selector_max_accel, self.gear_max_accel)

        # The following aren't very interesting for MMU control so leave to klipper defaults
        self.requested_accel_to_decel = config.getfloat('max_accel_to_decel', self.max_accel * 0.5, above=0.)
        self.max_accel_to_decel = self.requested_accel_to_decel
        self.square_corner_velocity = config.getfloat('square_corner_velocity', 5., minval=0.)
        self.junction_deviation = 0.
        self._calc_junction_deviation()
        # Print time tracking
        self.buffer_time_low = config.getfloat('buffer_time_low', 1.000, above=0.)
        self.buffer_time_high = config.getfloat('buffer_time_high', 2.000, above=self.buffer_time_low)
        self.buffer_time_start = config.getfloat('buffer_time_start', 0.250, above=0.)
        self.move_flush_time = config.getfloat('move_flush_time', 0.050, above=0.)
        self.print_time = 0.
        self.special_queuing_state = "Flushed"
        self.need_check_stall = -1.
        self.flush_timer = self.reactor.register_timer(self._flush_handler)
        self.move_queue.set_flush_time(self.buffer_time_high)
        self.idle_flush_print_time = 0.
        self.print_stall = 0
        self.drip_completion = None
        # Kinematic step generation scan window time tracking
        self.kin_flush_delay = SDS_CHECK_TIME
        self.kin_flush_times = []
        self.force_flush_time = self.last_kin_move_time = 0.
        # Setup iterative solver
        ffi_main, ffi_lib = chelper.get_ffi()
        self.trapq = ffi_main.gc(ffi_lib.trapq_alloc(), ffi_lib.trapq_free)
        self.trapq_append = ffi_lib.trapq_append
        self.trapq_finalize_moves = ffi_lib.trapq_finalize_moves
        self.step_generators = []
        # Create kinematics class
        gcode = self.printer.lookup_object('gcode')
        self.Coord = gcode.Coord
        self.extruder = DummyExtruder(self.printer) # PAUL: What if this was a real extruder (for gear)

        # Setup extruder kinematics for when gear rail is synced to extruder
        ffi_main, ffi_lib = chelper.get_ffi()
        self.sk_extruder = ffi_main.gc(ffi_lib.extruder_stepper_alloc(), ffi_lib.free)

        # Normal gear rail kinematics
        ffi_main, ffi_lib = chelper.get_ffi()
        self.sk_default = ffi_main.gc(ffi_lib.cartesian_stepper_alloc(b'y'), ffi_lib.free)
        
        # Fix kinematics to MMU type
        try:
            self.kin = MmuKinematics(self, config)
        except config.error as e:
            raise
        except self.printer.lookup_object('pins').error as e:
            raise
        except:
            msg = "Error loading MMU kinematics"
            logging.exception(msg)
            raise config.error(msg)
    
    def set_position(self, newpos, homing_axes=()):
        for _ in range(4 - len(newpos)):
            newpos.append(0.)
        logging.info("PAUL: in set_position, newpos=%s" % newpos)
        super(MmuToolHead, self).set_position(newpos, homing_axes)
    
    def get_selector_limits(self):
        return self.selector_max_velocity, self.selector_max_accel

    def get_gear_limits(self):
        return self.gear_max_velocity, self.gear_max_accel

    def fill_coord(self, coord):
        # Fill in any None entries in 'coord' with current toolhead position
        thcoord = list(self.toolhead.get_position())
        for i in range(len(coord)):
            if coord[i] is not None:
                thcoord[i] = coord[i]
        return thcoord

    # Is gear rail synced to extruder (for in print syncing)
    def is_gear_synced_to_extruder(self):
        return self.gear_motion_queue is not None

    # Is extruder stepper synced to gear rail (general MMU synced movement)
    def is_extruder_synced_to_gear(self):
        return self.extruder_synced_to_gear is not None

    def is_synced(self):
        return self.is_gear_synced_to_extruder() or self.is_extruder_synced_to_gear()

    def sync_gear_to_extruder(self, extruder_name):
        if self.extruder_synced_to_gear:
            self.sync_extruder_to_gear(None) # Mutually exclusive so unsync first

        printer_toolhead = self.printer.lookup_object('toolhead')
        printer_toolhead.flush_step_generation()
        self.flush_step_generation()
        gear_rail = self.get_kinematics().rails[1]

        if extruder_name:
            # Syncing
            if self.gear_motion_queue: return
            extruder = self.printer.lookup_object(extruder_name, None)
            if extruder is None or not isinstance(extruder, PrinterExtruder):
                raise self.printer.command_error("'%s' is not a valid extruder" % extruder_name)
            #extruder_stepper = extruder.extruder_stepper.stepper # PAUL

            for s in gear_rail.get_steppers():
                s.set_stepper_kinematics(self.sk_extruder)
            gear_rail.set_trapq(extruder.get_trapq())
            #gear_rail.set_position([0., extruder.last_position, 0.])
            gear_rail.set_position([extruder.last_position, 0., 0.])

            # Shift gear rail step generator to printer toolhead. Each stepper is registered individually
            for s in gear_rail.get_steppers():
                handler = s.generate_steps
                self.step_generators.remove(handler)
                printer_toolhead.register_step_generator(handler)
                logging.info("PAUL: shifted to printer toolhead gear_rail_handler=%s" % handler)

            self.gear_motion_queue = extruder_name # We are synced!
        else:
            # Unsyncing
            if not self.gear_motion_queue: return
            for s in gear_rail.get_steppers():
                s.set_stepper_kinematics(self.sk_default)
            gear_rail.set_trapq(self.get_trapq())
            gear_rail.set_position([0., 0., 0.])

            # Shift gear rail steppers step generator back to MMU toolhead
            for s in gear_rail.get_steppers():
                handler = s.generate_steps
                printer_toolhead.step_generators.remove(handler)
                self.register_step_generator(handler)
                logging.info("PAUL: shifted back to gear rail gear_rail_handler=%s" % handler)

            self.gear_motion_queue = None

    def sync_extruder_to_gear(self, extruder_name):
        if self.gear_motion_queue:
            self.sync_gear_to_extruder(None) # Mutually exclusive so unsync first

        printer_toolhead = self.printer.lookup_object('toolhead')
        printer_toolhead.flush_step_generation()
        self.flush_step_generation()
        gear_rail = self.get_kinematics().rails[1]

        if extruder_name:
            # Syncing
            if self.extruder_synced_to_gear: return
            extruder = self.printer.lookup_object(extruder_name, None)
            if extruder is None or not isinstance(extruder, PrinterExtruder):
                raise self.printer.command_error("'%s' is not a valid extruder" % extruder_name)
            extruder_stepper = extruder.extruder_stepper.stepper

            # Injecting the extruder stepper into the gear rail
            gear_rail.steppers.append(extruder_stepper)

            # Switch extruder stepper to use MMU toolhead kinematics and trapq
            self.prev_sk = extruder_stepper.set_stepper_kinematics(self.sk_default)
            self.prev_trapq = extruder_stepper.set_trapq(self.get_trapq())
            extruder_stepper.set_position([0., gear_rail.get_commanded_position(), 0.])

            # Shift extruder step generator to mmu toolhead
            handler = extruder_stepper.generate_steps
            printer_toolhead.step_generators.remove(handler)
            self.register_step_generator(handler)

            self.extruder_synced_to_gear = extruder_name # We are synced!
        else:
            # Unsyncing
            if not self.extruder_synced_to_gear: return
            extruder = self.printer.lookup_object(self.extruder_synced_to_gear)
            extruder_stepper = extruder.extruder_stepper.stepper

            # Restore gear rail steppers
            gear_rail.steppers.pop()

            # Restore extruder kinematics and trap queue
            extruder_stepper.set_trapq(self.prev_trapq)
            extruder_stepper.set_stepper_kinematics(self.prev_sk)
            extruder_stepper.set_position([0., 0., 0.])

            # Shift extruder step generator back to printer toolhead
            handler = extruder_stepper.generate_steps
            self.step_generators.remove(handler)
            printer_toolhead.register_step_generator(handler)

            self.extruder_synced_to_gear = None #

    def get_status(self, eventtime):
        res = super(MmuToolHead, self).get_status(eventtime)
        res.update(dict(self.get_kinematics().get_status(eventtime)))
        res.update({ 'filament_pos': self.mmu_toolhead.get_position()[1] })
        return res

# MMU Kinematics class
# (loosely based on corexy.py)
class MmuKinematics:
    def __init__(self, toolhead, config):
        self.printer = config.get_printer()

        # Setup "axis" rails
        self.axes = [('x', 'selector', True), ('y', 'gear', False)]
        self.rails = [MmuLookupMultiRail(config.getsection('stepper_mmu_' + s), need_position_minmax=mm, default_position_endstop=0.) for a, s, mm in self.axes]
        for rail, axis in zip(self.rails, 'xy'):
            rail.setup_itersolve('cartesian_stepper_alloc', axis.encode())

        for s in self.get_steppers():
            s.set_trapq(toolhead.get_trapq())
            toolhead.register_step_generator(s.generate_steps)

        self.printer.register_event_handler("stepper_enable:motor_off", self._motor_off)

        # Setup boundary checks
        self.selector_max_velocity, self.selector_max_accel = toolhead.get_selector_limits()
        self.gear_max_velocity, self.gear_max_accel = toolhead.get_gear_limits()
        self.limits = [(1.0, -1.0)] * len(self.rails)
    
    def get_steppers(self):
        return [s for rail in self.rails for s in rail.get_steppers()]

    def calc_position(self, stepper_positions):
        return [stepper_positions[rail.get_name()] for rail in self.rails]

    def set_position(self, newpos, homing_axes):
        for i, rail in enumerate(self.rails):
            rail.set_position(newpos)
            if i in homing_axes:
                self.limits[i] = rail.get_range()
    
    def home(self, homing_state):
        for axis in homing_state.get_axes():
            if not axis == 0: # Saftey: Only selector (axis[0]) can be homed
                continue
            rail = self.rails[axis]
            position_min, position_max = rail.get_range()
            hi = rail.get_homing_info()
            homepos = [None, None, None, None]
            homepos[axis] = hi.position_endstop
            forcepos = list(homepos)
            if hi.positive_dir:
                forcepos[axis] -= 1.5 * (hi.position_endstop - position_min)
            else:
                forcepos[axis] += 1.5 * (position_max - hi.position_endstop)
            homing_state.home_rails([rail], forcepos, homepos) # Perform homing

    def _motor_off(self, print_time):
        self.limits = [(1.0, -1.0)] * len(self.rails)

    def _check_endstops(self, move): # PAUL need this?
        end_pos = move.end_pos
        for i in range(len(self.rails)):
            if (move.axes_d[i]
                and (end_pos[i] < self.limits[i][0]
                     or end_pos[i] > self.limits[i][1])):
                if self.limits[i][0] > self.limits[i][1]:
                    raise move.move_error("Must home axis first")
                raise move.move_error()

    def check_move(self, move):
        limits = self.limits
        xpos, ypos = move.end_pos[:2]
# PAUL        if (xpos < limits[0][0] or xpos > limits[0][1] or ypos < limits[1][0] or ypos > limits[1][1]):
        if xpos < limits[0][0] or xpos > limits[0][1]:
            self._check_endstops(move) # PAUL need this?
        
        if move.axes_d[0]: # Selector
            logging.info("PAUL: move.limit_speed(%s, %s)" % (self.selector_max_velocity, self.selector_max_accel))
            move.limit_speed(self.selector_max_velocity, self.selector_max_accel)
        elif move.axes_d[1]: # Gear
            logging.info("PAUL: move.limit_speed(%s, %s)" % (self.gear_max_velocity, self.gear_max_accel))
            move.limit_speed(self.gear_max_velocity, self.gear_max_accel)

    def get_status(self, eventtime):
        return {
            'selector_homed': self.limits[0][0] <= self.limits[0][1],
            'gear_synced_to_extruder': self.is_gear_synced_to_extruder(),
            'extruder_synced_to_gear': self.is_extruder_synced_to_gear()
        }

# Extend Klipper homing module to leverage MMU "toolhead"
# (code pulled from homing.py)
class MmuHoming(Homing, object):
    def __init__(self, printer, mmu_toolhead):
        super(MmuHoming, self).__init__(printer)
        self.toolhead = mmu_toolhead # Override default toolhead
    
    def home_rails(self, rails, forcepos, movepos):
        # Notify of upcoming homing operation
        self.printer.send_event("homing:home_rails_begin", self, rails)
        # Alter kinematics class to think printer is at forcepos
        homing_axes = [axis for axis in range(3) if forcepos[axis] is not None]
        startpos = self._fill_coord(forcepos)
        homepos = self._fill_coord(movepos)
        self.toolhead.set_position(startpos, homing_axes=homing_axes)
        # Perform first home
        endstops = [es for rail in rails for es in rail.get_endstops()]
        logging.info("PAUL: endstops=%s" % endstops)
        hi = rails[0].get_homing_info()
        hmove = HomingMove(self.printer, endstops, self.toolhead) # Override default toolhead
        hmove.homing_move(homepos, hi.speed)
        # Perform second home
        if hi.retract_dist:
            # Retract
            startpos = self._fill_coord(forcepos)
            homepos = self._fill_coord(movepos)
            axes_d = [hp - sp for hp, sp in zip(homepos, startpos)]
            move_d = math.sqrt(sum([d*d for d in axes_d[:3]]))
            retract_r = min(1., hi.retract_dist / move_d)
            retractpos = [hp - ad * retract_r
                          for hp, ad in zip(homepos, axes_d)]
            self.toolhead.move(retractpos, hi.retract_speed)
            # Home again
            startpos = [rp - ad * retract_r
                        for rp, ad in zip(retractpos, axes_d)]
            self.toolhead.set_position(startpos)
            hmove = HomingMove(self.printer, endstops, self.toolhead) # Override default toolhead
            hmove.homing_move(homepos, hi.second_homing_speed)
            if hmove.check_no_movement() is not None:
                raise self.printer.command_error(
                    "Endstop %s still triggered after retract"
                    % (hmove.check_no_movement(),))
        # Signal home operation complete
        self.toolhead.flush_step_generation()
        self.trigger_mcu_pos = {sp.stepper_name: sp.trig_pos
                                for sp in hmove.stepper_positions}
        self.adjust_pos = {}
        self.printer.send_event("homing:home_rails_end", self, rails)
        if any(self.adjust_pos.values()):
            # Apply any homing offsets
            kin = self.toolhead.get_kinematics()
            homepos = self.toolhead.get_position()
            kin_spos = {s.get_name(): (s.get_commanded_position()
                                       + self.adjust_pos.get(s.get_name(), 0.))
                        for s in kin.get_steppers()}
            newpos = kin.calc_position(kin_spos)
            for axis in homing_axes:
                homepos[axis] = newpos[axis]
            self.toolhead.set_position(homepos)

# Extend PrinterRail to allow for multiple (switchable) endstops and allow no default endstop
# (defined in stepper.py)
class MmuPrinterRail(stepper.PrinterRail, object):
    def __init__(self, config, **kwargs):
        logging.info("PAUL: MmuPrinterRail.init(), name=%s" % config.get_name())
        self.printer = config.get_printer()
        self.rail_name = config.get_name()
        self.query_endstops = self.printer.load_object(config, 'query_endstops')
        self.extra_endstops = []
        self.virtual_endstops = []
        self._in_init = True
        super(MmuPrinterRail, self).__init__(config, **kwargs)
        self._in_init = False

        # Setup default endstop similarly to "extra" endstops with vanity sensor name
        endstop_pin = config.get('endstop_pin', None)
        endstop_name = config.get('endstop_name', None)
        if endstop_pin and endstop_name:
            # Remove the default endstop name if alternative name is specified
            qee = self.query_endstops.endstops
            if qee:
                qee.pop()
            # I don't see the value is registering virtual endstops(?)
            if 'virtual_endstop' in endstop_pin:
                self.virtual_endstops.append(name)
            else:
                self.query_endstops.register_endstop(self.endstops[0][0], endstop_name)

        # Add useful debugging command
        gcode = self.printer.lookup_object("gcode")
        gcode.register_mux_command('DUMP_RAIL', "RAIL", config.get_name(), self.cmd_DUMP_RAIL, desc=self.cmd_DUMP_RAIL_help)

    def add_extra_stepper(self, config, **kwargs):
        logging.info("PAUL: add_extra_stepper()")
        mock = False
        if self._in_init and not self.endstops and config.get('endstop_pin', None) is None:
            # No endstop defined, so configure a mock endstop. The rail is, of course, only homable
            # if it has a properly configured endstop at runtime
            self.endstops = [(self.MockEndstop(), "mock")] # Hack: pretend we have a default endstop so super class will work
            mock = True
        super(MmuPrinterRail, self).add_extra_stepper(config, **kwargs)

        # Handle any extra endstops
        extra_endstop_pins = config.getlist('extra_endstop_pins', [])
        extra_endstop_names = config.getlist('extra_endstop_names', [])
        if extra_endstop_pins:
            if len(extra_endstop_pins) != len(extra_endstop_names):
                raise self.config.error("`extra_endstop_pins` and `extra_endstop_names` are different lengths")
            for idx, pin in enumerate(extra_endstop_pins):
                name = extra_endstop_names[idx]
                if 'virtual_endstop' in pin:
                    self.virtual_endstops.append(name)
                self.add_extra_endstop(pin, name)

    def add_extra_endstop(self, pin, name, register=True):
        logging.info("PAUL: add_extra_endstop(pin=%s, name=%s, register=%s)" % (pin, name, register))
        ppins = self.printer.lookup_object('pins')
        mcu_endstop = ppins.setup_pin('endstop', pin)
        self.extra_endstops.append((mcu_endstop, name))
        for s in self.steppers:
            mcu_endstop.add_stepper(s)
        if register and not self.is_endstop_virtual(name):
            self.query_endstops.register_endstop(mcu_endstop, name)
        return mcu_endstop

    def get_extra_endstop_names(self):
        return [x[1] for x in self.extra_endstops]

    # Returns the mcu_endstop of given name
    def get_extra_endstop(self, name):
         matches = [x for x in self.extra_endstops if x[1] == name]
         if matches:
             logging.info("PAUL: matches=%s, [0]=%s, [0][0]=%s, list=%s" % (matches, matches[0], matches[0][0], list(matches)))
             return list(matches)
         else:
             return None

    def is_endstop_virtual(self, name):
        return name in self.virtual_endstops if name else False

    cmd_DUMP_RAIL_help = "For debugging: dump configuration of rail with multiple endstops"
    def cmd_DUMP_RAIL(self, gcmd):
        msg = self.dump_stepper()
        gcmd.respond_raw(msg)

    def dump_stepper(self):
        msg = "Rail: %s\n" % self.rail_name
        msg += "- Num steppers: %d\n" % len(self.steppers)
        msg += "- Num default endstops: %d\n" % len(self.endstops)
        msg += "- Num extra endstops: %d\n" % len(self.extra_endstops)
        msg += "Steppers:\n"
        for idx, s in enumerate(self.get_steppers()):
            msg += "- Stepper %d: %s\n" % (idx, s.get_name())
            msg += "- - Commanded Position: %.2f\n" % s.get_commanded_position()
            msg += "- - MCU Position: %.2f\n" % s.get_mcu_position()
            msg += "- - Rotation Distance: %.6f (in %d steps)\n" % s.get_rotation_distance()
        msg += "Endstops:\n"
        for (mcu_endstop, name) in self.endstops:
            if mcu_endstop.__class__.__name__ == "MockEndstop":
                msg += "- None (Mock - cannot home rail)\n"
            else:
                msg += "- '%s', mcu: '%s', pin: '%s', obj_id: %s" % (name, mcu_endstop.get_mcu().get_name(), mcu_endstop._pin, id(mcu_endstop))
                msg += " (virtual)\n" if self.is_endstop_virtual(name) else "\n"
                msg += "- - Registed on steppers: %s\n" % ["%d: %s" % (idx, s.get_name()) for idx, s in enumerate(mcu_endstop.get_steppers())]
        msg += "Extra Endstops:\n"
        for (mcu_endstop, name) in self.extra_endstops:
            msg += "- '%s', mcu: '%s', pin: '%s', obj_id: %s" % (name, mcu_endstop.get_mcu().get_name(), mcu_endstop._pin, id(mcu_endstop))
            msg += " (virtual)\n" if self.is_endstop_virtual(name) else "\n"
            msg += "- - Registed on steppers: %s\n" % ["%d: %s" % (idx, s.get_name()) for idx, s in enumerate(mcu_endstop.get_steppers())]
# PAUL
#        if self.__class__.__name__ == "ManualExtruderStepper" and self.is_synced():
#            msg += "Synced to extruder '%s'" % self.synced_extruder_name
        return msg

    class MockEndstop:
        def add_stepper(self, *args, **kwargs):
            pass

# Wrapper for dual stepper motor support
def MmuLookupMultiRail(config, need_position_minmax=True, default_position_endstop=None, units_in_radians=False):
    rail = MmuPrinterRail(config, need_position_minmax=need_position_minmax, default_position_endstop=default_position_endstop, units_in_radians=units_in_radians)
    for i in range(1, 99):
        if not config.has_section(config.get_name() + str(i)):
            break
        rail.add_extra_stepper(config.getsection(config.get_name() + str(i)))
    return rail
