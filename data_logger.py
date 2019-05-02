from os.path import join, dirname, realpath
from multiprocessing import Value
from threading import Thread
from time import sleep, time
from ctypes import c_bool
import ingenialink as il
import datetime
import argparse
import asyncio
import csv
import sys


class ReadThread(Thread):
    def __init__(self, parent, servo, refresh_time, close_activated):
        """ Constructor, setting initial variables """
        super(ReadThread, self).__init__()
        self.__parent = parent
        self.__servo = servo
        self.__refresh_time = refresh_time
        self.__close_activated = close_activated
        self.__futures = []
        self.__loop = asyncio.new_event_loop()

    def run(self):
        """ Main control loop """
        self.__loop.run_until_complete(asyncio.wait(self.__futures))

    def add_task(self, key, shared_variable):
        async def callback(servo, key, shared_variable, refresh_time, close_activated):
            while not close_activated.value:
                t_start = float(time())
                try:
                    shared_variable.value = servo.raw_read(key)
                except:
                    pass
                t_sleep = refresh_time - (float(time()) - t_start)
                await asyncio.sleep(t_sleep)

        self.__futures.append(asyncio.ensure_future(
            callback(self.__servo, key, shared_variable, self.__refresh_time, self.__close_activated),
            loop=self.__loop
        ))


class LogDataThread(Thread):
    def __init__(self, servo, refresh_time, registers_data, registers_to_read, ready_to_read, data_log_file, data_log_writer, close_activated):
        """ Constructor, setting initial variables """
        super(LogDataThread, self).__init__()
        self.__servo = servo
        self.__refresh_time = refresh_time
        self.__registers_data = registers_data
        self.__registers_to_read = registers_to_read
        self.__ready_to_read = ready_to_read
        self.__data_log_file = data_log_file
        self.__data_log_writer = data_log_writer
        self.__close_activated = close_activated
        self.__loop = asyncio.new_event_loop()

    def run(self):
        """ Main control loop """
        async def callback(servo, refresh_time, registers_data, registers_to_read, data_log_file, data_log_writer, close_activated):
            # Init aux variables
            while not close_activated.value:
                # Read all registers and save to the csv
                t_start = float(time())
                new_row = [datetime.datetime.now().strftime("%Y-%m-%dT%H:%M:%S.%f")]
                for register_to_read in registers_to_read:
                    new_row.append(registers_data[register_to_read].value)
                # Write row to csv
                data_log_writer.writerow(new_row)
                data_log_file.flush()
                # Wait until next iteration
                t_sleep = refresh_time - (float(time()) - t_start)
                if t_sleep > 0:
                    sleep(t_sleep)

        # Wait until ready to read is set to True
        while not self.__ready_to_read.value and not self.__close_activated.value:
            sleep(0.2)
        if not self.__close_activated.value:
            print('Starting to log data...')
            sys.stdout.flush()
            fut = asyncio.ensure_future(
                callback(self.__servo, self.__refresh_time, self.__registers_data, self.__registers_to_read, self.__data_log_file, self.__data_log_writer, self.__close_activated),
                loop=self.__loop
            )
            self.__loop.run_until_complete(asyncio.wait([fut]))


class ControlThread(Thread):
    def __init__(self, parent, servo, position_1, position_2, position_tolerance, ready_to_read, close_activated):
        """ Constructor, setting initial variables """
        super(ControlThread, self).__init__()
        self.__parent = parent
        self.__servo = servo
        self.__position_1 = position_1
        self.__position_2 = position_2
        self.__position_tolerance = position_tolerance
        self.__ready_to_read = ready_to_read
        self.__close_activated = close_activated

    def disable_motor(self):
        try:
            self.__servo.disable()
            print("Motor disabled")
            sys.stdout.flush()
        except Exception as e:
            print("Error trying to disable.", str(e))
            sys.exit(0)

    def target_latch(self):
        control_word = int(self.__servo.read("CONTROL_WORD"))
        new_control_word = control_word & ~0b1000000000
        self.__servo.write("CONTROL_WORD", new_control_word)
        new_control_word = control_word | 0b1000000000
        self.__servo.write("CONTROL_WORD", new_control_word)

    def wait_until_position_achieved(self, position_required):
        position_ready = abs(self.__servo.read('ACTUAL_POSITION') - position_required) < self.__position_tolerance
        while not position_ready and not self.__close_activated.value:
            position_ready = abs(self.__servo.read('ACTUAL_POSITION') - position_required) < self.__position_tolerance
            sleep(0.2)

    def set_position_required(self, position_required):
        # Set position to the position required
        self.__servo.write('POSITION_SET-POINT', position_required)
        # Target latch
        self.target_latch()
        # Wait until motor arrives to the first position
        self.wait_until_position_achieved(position_required)

    def run(self):
        """ Main control loop """
        # Disable motor before start
        self.disable_motor()
        # Set Operation mode in Profile position
        self.__servo.write('MODE_OF_OPERATION', 20)
        try:
            self.__servo.enable()
            print("Motor enabled")
        except Exception as e:
            print("Error trying to enable.", str(e))
            self.disable_motor()
        sys.stdout.flush()
        # Set position to the first position
        self.set_position_required(self.__position_1)
        # Ready to start readings
        self.__ready_to_read.value = True
        # Init aux variables
        last_position_achieved = self.__position_1
        while not self.__close_activated.value:
            try:
                if last_position_achieved == self.__position_1:
                    # Case 1: Position 1 -> Position 2
                    self.set_position_required(self.__position_2)
                    last_position_achieved = self.__position_2
                else:
                    # Case 2: Position 2 -> Position 1
                    self.set_position_required(self.__position_1)
                    last_position_achieved = self.__position_1
            except Exception as e:
                print("Error at movement.", str(e))
                sys.stdout.flush()
                self.disable_motor()
        self.disable_motor()


class SummitDataLogger(object):
    def __init__(self, args):
        # Get data from args
        self.__ip = args.ip
        self.__port = args.port
        self.__movement = args.movement
        self.__position_1 = args.position_1
        self.__position_2 = args.position_2
        self.__position_tolerance = args.position_tolerance
        self.__refresh_time = args.refresh_time/1000.0
        # Init internal variables
        self.__servo = None
        self.__read_thread = None
        self.__registers_data = dict()
        self.__data_log_file = None
        self.__data_log_writer = None
        self.__ready_to_read = Value(c_bool, False)
        self.__close_activated = Value(c_bool, False)
        # Registers to read
        self.__registers_to_read = [          
            "POSITION_SET-POINT",
            "POSITION_DEMAND",
            "ACTUAL_POSITION",
            "VELOCITY_SET-POINT",
            "VELOCITY_DEMAND",
            "ACTUAL_VELOCITY",
            "DIGITAL_HALL_VALUE",
            "DIGITAL_ENCODER_VALUE",
            "BUS_VOLTAGE_READINGS",
            "MOTOR_TEMPERATURE",
            "CURRENT_A",
            "CURRENT_B",
            "CURRENT_C",
            "POW_STAGE_TEMP",
            "GEN._VOLTAGE_PHASE_A",
            "GEN._VOLTAGE_PHASE_B",
            "GEN._VOLTAGE_PHASE_C"
        ]

    def init_variables(self):
        # Get the dictionary path to load
        dictionary_path = join(dirname(realpath(__file__)), 'resources', 'summit.xml')
        # Create the net and servo
        try:
            _, self.__servo = il.lucky(il.NET_PROT.ETH, dictionary_path, self.__ip, port_ip=self.__port)
        except Exception as e:
            print("Error trying to connect to the drive.", str(e))
            sys.stdout.flush()
            sys.exit(0)
        sys.stdout.flush()
        # Init read thread
        self.__read_thread = ReadThread(self, self.__servo, self.__refresh_time, self.__close_activated)
        for register_to_read in self.__registers_to_read:
            self.__registers_data[register_to_read] = Value('d', 0)
            self.__read_thread.add_task(register_to_read, self.__registers_data[register_to_read])
        # Init write thread
        # Create the csv file to log the data
        self.__data_log_file = open('./outputs/data_log.csv', 'w', newline='')
        self.__data_log_writer = csv.writer(self.__data_log_file)
        # Set headers to the csv
        headers = ['Timestamp']
        for register_to_read in self.__registers_to_read:
            headers.append(self.__servo.dict.regs[register_to_read].labels["en_US"])
        self.__data_log_writer.writerow(headers)
        self.__log_data_thread = LogDataThread(self.__servo, self.__refresh_time, self.__registers_data, self.__registers_to_read, self.__ready_to_read, self.__data_log_file, self.__data_log_writer, self.__close_activated)
        self.__log_data_thread.start()

    def run(self):
        self.init_variables()
        # Start thread in order to read parameters
        self.__read_thread.start()
        # Wait for the first iteration read
        sleep(self.__refresh_time)
        # Check if the movement is required
        if self.__movement:
            # Init control thread
            self.__control_thread = ControlThread(self, self.__servo, self.__position_1, self.__position_2, self.__position_tolerance, self.__ready_to_read, self.__close_activated)
            self.__control_thread.start()
        else:
            self.__ready_to_read.value = True
        # Infinite loopx
        while not self.__close_activated.value:
            w = input("Type 'quit' to exit...\n")
            if w == 'quit':
                self.__close_activated.value = True
            else:
                sleep(0.2)
        sys.exit(0)


def str2bool(v):
    if v.lower() in ('yes', 'true', 't', 'y', '1'):
        return True
    elif v.lower() in ('no', 'false', 'f', 'n', '0'):
        return False
    else:
        raise argparse.ArgumentTypeError('Boolean value expected.')

def main(args):
    summit_data_logger = SummitDataLogger(args)
    summit_data_logger.run()

if __name__ == '__main__':
    # Usage
    parser = argparse.ArgumentParser(description='Summit Data Logger.')
    parser.add_argument('--ip', metavar="", type=str, help='the ip of the drive. 192.168.2.22 by default', default='192.168.2.22')
    parser.add_argument('--port', metavar="", type=int, help='the port of the drive. 23 by default', default=23)
    parser.add_argument('--refresh_time', metavar="", type=float, help='refresh time to read values in milliseconds. 100ms by default', default=100)
    parser.add_argument('--movement', metavar="", type=str2bool, help='movement between positions activated. False by default', default=True)
    parser.add_argument('--position_1', metavar="", type=int, help='the first position required in counts. 0 by default', default=0)
    parser.add_argument('--position_2', metavar="", type=int, help='the second position required in counts. 65535 by default', default=65535)
    parser.add_argument('--position_tolerance', metavar="", type=int, help='tolerance position in counts. 200cnts by default', default=200)
    args = parser.parse_args()
    main(args)