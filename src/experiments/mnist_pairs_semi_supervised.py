import torch

from inference.graphical_model.learn.graphical_model_sgd_learner import GraphicalModelSGDLearner
from inference.graphical_model.representation.factor.fixed.fixed_pytorch_factor import FixedPyTorchTableFactor
from inference.graphical_model.representation.factor.neural.neural_factor import NeuralFactor
from inference.graphical_model.representation.factor.pytorch_table_factor import PyTorchTableFactor
from inference.graphical_model.representation.table.pytorch_log_table import PyTorchLogTable
from inference.graphical_model.variable.integer_variable import IntegerVariable
from inference.graphical_model.variable.tensor_variable import TensorVariable
from inference.neural_net.ConvNet import ConvNet
from inference.neural_net.MLP import MLP
from inference.neural_net.from_log_to_probabilities_adapter import FromLogToProbabilitiesAdapter
from util.data_loader_from_random_data_point_thunk import data_loader_from_random_data_point_generator

# Trains a digit recognizer with the following factor graph:
#
#                                 Constraint
#                                      |
#                           +---------------------+
#                           |   Constraint <=>    |
#             Digit0  ------| Digit1 = Digit0 + 1 |------- Digit1
#               |           +---------------------+          |
#               |                                            |
#      +------------------+                         +------------------+
#      | Digit recognizer |                         | Digit recognizer |
#      +------------------+                         +------------------+
#               |                                            |
#               |                                            |
#             Image0                                       Image1
#
# that is to say, five variables Constraint, Digit0, Digit1, Image0, Image1,
# where Digit1 is constrained to be Digit0 + 1 iff Constraint is true,
# and Digit_i is the recognition of Image_i.
#
# We anticipate that presenting pairs images of consecutive digits to the model,
# querying Constraint and minimizing its epoch_average_loss in comparison to the expected value "true"
# will train the recognizer (a shared neural net applied to both images)
# to recognize MNIST digits.
#
# The idea is that images for 0 will only appear in Image0, and images for 9 only in Image1,
# and this will be a strong enough signal for the recognizer to learn those images.
# Once that happens, those images work as the signal for 1 and 8, which work as signals for 2 and 7
# and so on, until all digits are learned.
#
# This script offers multiple ways of running the above, including a simplified setting
# in which the "images" are just integers from 0 to 9.
# Even in this very simple setting, the recognizer still needs to learn to associate each digit to itself.
# This provides a way of testing the general dynamic of the model without dealing with the complexities
# of actual image recognition.
#
# Other options include whether the two recognizers are the same network or not
# (essential for learning from positive examples only),
# whether negative examples are present (non-consecutive digits with Constraint = false),
# whether to use a single digit image per digit,
# and various possible initializations for the recognizer.
from util.generic_sgd_learner import default_after_epoch, GenericSGDLearner

from util.mnist_util import read_mnist, show_images_and_labels

from util.util import join, set_default_tensor_type_and_return_device

# -------------- PARAMETERS

number_of_digits = 10
chain_length = 2
use_real_images = False  # use real images; otherwise, use its digit value only as input (simpler version of experiment)
use_conv_net = False # if use_real_images, neural net used is ConvNet for MNIST; otherwise, a simpler MLP for MNIST.
show_examples = False  # show some examples of images (sanity check for data structure)
use_a_single_image_per_digit = True  # to make the problem easier -- removes digit variability from the problem
try_cuda = True
batch_size = 200
number_of_batches_between_updates = 1  # batches have two functions: how much to fit into CUDA if using it, and
                                         # how many examples to observe before updating.
                                         # Here we are splitting those two functions, leaving batch_size for the
                                         # number of datapoints processed at a time, but allowing for updating
                                         # only after a number of batches are processed.
                                         # This allows for a better estimation of gradients at each update,
                                         # decreasing the influence of random fluctuations in these estimates for
                                         # each update, but may make learning much slower
number_of_batches_per_epoch = 100
number_of_epochs_between_evaluations = 1
max_real_mnist_datapoints = None
number_of_digit_instances_in_evaluation = 1000
seed = None   # use None for non-deterministic seed

use_positive_examples_only = True  # show only examples of consecutive pairs with the constraint labeled True,
# as opposed to random pairs with constraint labeled True or False accordingly.
# Including negative examples makes the problem easier, but less
# uniform random tables still get stuck in local minima.

use_shared_recognizer = True  # the same recognizer is shared by both "image" -> digit pairs
# Note that, if given positive examples only, we need a shared recognizer for
# learning to have a chance of succeeding
# because otherwise the two recognizers never get to see 9 and 0 respectively.

# 'recognizer_type' below allows the selection of different functions and initializations for recognizer.
# If use_real_images is True, this selection is ignored and a ConvNet or MLP is used, along with number_of_digits = 10.

recognizer_type = "neural net"  # if use_real_images, a ConvNet or MLP, depending on option use_conv_net
                            # if use_real_images == False, an MLP with a single input unit, number_of_digits hidden units,
                            # and number_of_digits output units.
#recognizer_type = "fixed ground truth table"  # the correct table, with fixed parameters.
#recognizer_type = "random table"  # an initialization with random potentials; see parameter below for increasing randomness
#recognizer_type = "noisy left-shift"  # a hard starting point, in which digits map to their left-shift.
                                   # This will get most consecutive pairs to satisfy the constraint
                                   # but for (0, 1) and (8,9), even though *every* digit is being misclassified.
                                   # See parameter below for selecting noise level.
                                   # Making this noisier helps because it "dillutes" the hard starting point.
recognizer_type = "uniform"  # a uniform table -- learning works well


left_shift_noise = .1  # probability of noisy left-shift recognizer initialization not left-shifting
                       # but hitting some other digit uniformly

upper_bound_for_log_potential_in_random_table = 1  # log of potentials are uniformly sampled from
                                                   # [0, upper_bound_for_log_potential_in_random_table].
                                                   # The higher the value, the farther from the uniform the table is.
                                                   # So far we observe that tables farther from the uniform
                                                   # often get stuck in local minima,
                                                   # and that uniform tables always converge to the correct answer.

# -------------- END OF PARAMETERS


# -------------- PROCESSING PARAMETERS
if use_real_images:
    if number_of_digits != 10 or recognizer_type != "neural net":
        print("Using real images; forcing number of digits to 10 and recognizer type to neural net")
    number_of_digits = 10
    recognizer_type = "neural net"

if recognizer_type == "uniform":
    recognizer_type = "random table"
    upper_bound_for_log_potential_in_random_table = 0  # A value of 0 samples from [0, 0], providing a uniform table.

lr = 1e-3 if use_real_images else 1e-3
loss_decrease_tol = lr*.0001

if batch_size * number_of_batches_between_updates < 500:
    max_epochs_to_go_before_stopping_due_to_loss_decrease = 15
else:
    max_epochs_to_go_before_stopping_due_to_loss_decrease = 1

# -------------- END OF PROCESSING PARAMETERS


def main():

    set_seed()

    # Create random variables
    global image, digit, constraint  # so they are easily accessible in later functions
    image = []
    digit = []
    for i in range(chain_length):
        image.append(TensorVariable(f"image{i}") if use_real_images else IntegerVariable(f"image{i}", number_of_digits))
        digit.append(IntegerVariable(f"digit{i}", number_of_digits))
    constraint = IntegerVariable("constraint", 2)

    # Load images, if needed, before setting default device to cuda
    global from_digit_batch_to_image_batch
    if use_real_images:
        global images_by_digits_by_phase  # so they are easily accessible in later functions
        global next_image_index_by_digit
        images_by_digits_by_phase = read_mnist(max_real_mnist_datapoints)
        number_of_training_images = sum([len(images_by_digits_by_phase["train"][d]) for d in range(number_of_digits)])
        print(f"Loaded {number_of_training_images:,} training images")
        next_image_index_by_digit = {d: 0 for d in range(number_of_digits)}
        if show_examples:
            images = [images_by_digits_by_phase["train"][d][i] for i in range(5) for d in range(number_of_digits)]
            labels = [d for i in range(5) for d in range(number_of_digits)]
            show_images_and_labels(5, 10, images, labels)
        from_digit_batch_to_image_batch = get_next_real_image_batch_for_digit_batch
    else:
        from_digit_batch_to_image_batch = get_next_fake_image_batch_for_digit_batch

    train_data_loader = make_data_loader()

    device = set_default_tensor_type_and_return_device(try_cuda)
    print(f"Using {device} device")

    # Creating model after attempting to set default tensor type to cuda so it sits there
    global constraint_factor, i0_d0, i1_d1  # so they are easily accessible in later functions
    constraint_factor = make_constraint_factor()
    i0_d0, i1_d1 = make_recognizer_factors()

    global model
    model = [
        # IMPORTANT: this particular factor order is relied upon later in the code
        constraint_factor,
        i0_d0,
        i1_d1,
    ]

    print("\nInitial model:")
    print(join(model, "\n"))
    print("\nInitial evaluation:")
    print_digit_evaluation()

    if recognizer_type != "fixed ground truth table":
        print("Learning...")
        learner = \
            GraphicalModelSGDLearner(
                model, train_data_loader, device=device, lr=lr,
                loss_decrease_tol=loss_decrease_tol,
                max_epochs_to_go_before_stopping_due_to_loss_decrease=
                    max_epochs_to_go_before_stopping_due_to_loss_decrease,
                after_epoch=after_epoch)
        learner.learn()

    print("\nFinal model:")
    print(join(model, "\n"))
    print("\nFinal evaluation:")
    print_digit_evaluation()


def make_constraint_factor():
    constraint_predicate = lambda d0, d1, constraint: int(d1 == d0 + 1) == constraint
    constraint_factor = FixedPyTorchTableFactor.from_predicate((digit[0], digit[1], constraint), constraint_predicate)
    return constraint_factor


def make_recognizer_factors():
    if recognizer_type == "neural net":
        if use_real_images:
            if use_conv_net:
                print("Using ConvNet")
                neural_net_maker = lambda: FromLogToProbabilitiesAdapter(ConvNet())
            else:
                print("Using MLP for MNIST")
                def neural_net_maker():
                    net = MLP(28*28, number_of_digits, number_of_digits)
                    # # Set weights for as uniform as possible
                    # def set_to_one(m):
                    #     m.weight = torch.nn.Parameter(torch.ones(m.weight.size(), requires_grad=True))
                    # net.apply(set_to_one)
                    return net
        else:
            neural_net_maker = lambda: MLP(1, number_of_digits, number_of_digits)
        neural_net1 = neural_net_maker()
        neural_net2 = neural_net1 if use_shared_recognizer else neural_net_maker()

        uniform_pre_training(neural_net1)
        if not use_shared_recognizer:
            uniform_pre_training(neural_net2)

        i0_d0 = NeuralFactor(neural_net1, input_variables=[image[0]], output_variable=digit[0])
        i1_d1 = NeuralFactor(neural_net2, input_variables=[image[1]], output_variable=digit[1])

    elif recognizer_type == "fixed ground truth table":
        predicate = lambda i, d: d == i
        image_and_digit_table = PyTorchTableFactor.from_predicate([image[0], digit[0]], predicate, log_space=True).table
        i0_d0 = PyTorchTableFactor([image[0], digit[0]], image_and_digit_table)
        i1_d1 = PyTorchTableFactor([image[1], digit[1]], image_and_digit_table)

    elif recognizer_type == "noisy left-shift":
        probability_of_left_shift = 1 - left_shift_noise
        number_of_non_left_shift = number_of_digits - 1
        probability_of_each_non_left_shift = left_shift_noise / number_of_non_left_shift
        left_shift_pairs = {(i, (i - 1) % number_of_digits) for i in range(number_of_digits)}

        def potential(i, d):
            return probability_of_left_shift if (i, d) in left_shift_pairs else probability_of_each_non_left_shift

        if use_shared_recognizer:
            image_and_digit_table1 = PyTorchTableFactor.from_function([image[0], digit[0]], potential, log_space=True).table
            image_and_digit_table2 = image_and_digit_table1
        else:
            image_and_digit_table1 = PyTorchTableFactor.from_function([image[0], digit[0]], potential, log_space=True).table
            image_and_digit_table2 = PyTorchTableFactor.from_function([image[1], digit[1]], potential, log_space=True).table

        i0_d0 = PyTorchTableFactor([image[0], digit[0]], image_and_digit_table1)
        i1_d1 = PyTorchTableFactor([image[1], digit[1]], image_and_digit_table2)

    elif recognizer_type == "random table":
        def make_random_parameters():
            return (torch.rand(number_of_digits, number_of_digits)
                    * upper_bound_for_log_potential_in_random_table).requires_grad_(True)

        if use_shared_recognizer:
            image_and_digit_table1 = PyTorchLogTable(make_random_parameters())
            image_and_digit_table2 = image_and_digit_table1
        else:
            image_and_digit_table1 = PyTorchLogTable(make_random_parameters())
            image_and_digit_table2 = PyTorchLogTable(make_random_parameters())
        i0_d0 = PyTorchTableFactor([image[0], digit[0]], image_and_digit_table1)
        i1_d1 = PyTorchTableFactor([image[1], digit[1]], image_and_digit_table2)

    else:
        raise Exception(f"Unknown recognizer type: {recognizer_type}")

    return i0_d0, i1_d1


def make_data_loader():
    batch_generator = \
        random_positive_examples_batch_generator() if use_positive_examples_only \
            else random_positive_or_negative_examples_batch_generator()
    train_data_loader = data_loader_from_random_data_point_generator(number_of_batches_per_epoch, batch_generator, print=None)
    return train_data_loader


def get_next_fake_image_batch_for_digit_batch(digit_batch):
    return digit_batch


def get_next_real_image_batch_for_digit_batch(digit_batch):
    images_list = []
    for d in digit_batch:
        d = d.item()
        image = images_by_digits_by_phase["train"][d][next_image_index_by_digit[d]]
        if use_a_single_image_per_digit:
            pass  # leave the index at the first position forever
        else:
            next_image_index_by_digit[d] += 1
            if next_image_index_by_digit[d] == len(images_by_digits_by_phase["train"][d]):
                next_image_index_by_digit[d] = 0
        images_list.append(image)
    images_batch = torch.stack(images_list).to(digit_batch.device)
    return images_batch


def random_positive_or_negative_examples_batch_generator():
    if use_real_images:
        from_digit_batch_to_image_batch = get_next_real_image_batch_for_digit_batch
    else:
        from_digit_batch_to_image_batch = get_next_fake_image_batch_for_digit_batch

    def generator():
        d0_values = torch.randint(number_of_digits, (batch_size,))
        d1_values = torch.randint(number_of_digits, (batch_size,))
        i0_values = from_digit_batch_to_image_batch(d0_values)
        i1_values = from_digit_batch_to_image_batch(d1_values)
        constraint_values = (d1_values == d0_values + 1).long()
        random_pair_result = {image[0]: i0_values, image[1]: i1_values}, {constraint: constraint_values}
        # first_random_pair_result = {image[0]: i0_values[0], image[1]: i1_values[0]}, {constraint: constraint_values[0]}
        # print(first_random_pair_result)
        return random_pair_result
    return generator


def random_positive_examples_batch_generator():
    if use_real_images:
        from_digit_batch_to_image_batch = get_next_real_image_batch_for_digit_batch
    else:
        from_digit_batch_to_image_batch = get_next_fake_image_batch_for_digit_batch

    def generator():
        d0_values = torch.randint(number_of_digits - 1, (batch_size,))  # digit[0] is never equal to the last digit
        d1_values = d0_values + 1  # and digit[1] is never equal to 0
        i0_values = from_digit_batch_to_image_batch(d0_values)
        i1_values = from_digit_batch_to_image_batch(d1_values)
        random_constrained_pair_result = {image[0]: i0_values, image[1]: i1_values}, {constraint: torch.ones(batch_size).long()}
        # print(random_pair_result)
        return random_constrained_pair_result
    return generator


def after_epoch(learner):
    print()
    default_after_epoch(learner)
    if learner.epoch % number_of_epochs_between_evaluations == 0:
        print_digit_evaluation(learner)


def print_digit_evaluation(learner=None):
    constraint_factor, i0_d0, i1_d1 = model  # note that this relies on the factor order in the model
    from_i0_to_d0 = lambda v_i0: i0_d0.condition({image[0]: v_i0}).normalize().table_factor
    from_i1_to_d1 = lambda v_i1: i1_d1.condition({image[1]: v_i1}).normalize().table_factor
    with torch.no_grad():
        recognizers = [from_i0_to_d0] if use_shared_recognizer else [from_i0_to_d0, from_i1_to_d1]
        for recognizer in recognizers:
            print_posterior_of(recognizer, learner)


def print_posterior_of(recognizer, learner=None):
    for digit in range(number_of_digits):
        digit_batch = torch.full((number_of_digit_instances_in_evaluation,), digit)
        image_batch = from_digit_batch_to_image_batch(digit_batch)
        if learner is not None and learner.device is not None:
            image_batch = image_batch.to(learner.device)
        posterior_probability = recognizer(image_batch)
        if posterior_probability.batch:
            posterior_probability_tensor = posterior_probability.table.potentials_tensor().sum(0)
            posterior_probability_tensor /= posterior_probability_tensor.sum()
        else:
            posterior_probability_tensor = posterior_probability.table.potentials_tensor()
        print_posterior_tensor(digit, posterior_probability_tensor)


def print_posterior_tensor(digit, output_probability_tensor):
    image_description = 'image' if use_real_images else 'fake "image"'
    print(f"Prediction for {image_description} {digit}: {digit_distribution_tensor_str(output_probability_tensor)}")


def digit_distribution_tensor_str(tensor):
    return join([potential_str(potential) for potential in tensor])

def potential_str(potential):
    if potential < 1e-2:
        return "    "
    else:
        return f"{potential:0.2f}"


def set_seed():
    global seed
    if seed is None:
        seed = torch.seed()
    else:
        torch.manual_seed(seed)
    print(f"Seed: {seed}")


def uniform_pre_training(recognizer):
    def random_batch_generator():
        if use_real_images:
            from_digit_batch_to_image_batch = get_next_real_image_batch_for_digit_batch
        else:
            from_digit_batch_to_image_batch = get_next_fake_image_batch_for_digit_batch

        def generator():
            digits = torch.randint(number_of_digits, (batch_size,))
            images = from_digit_batch_to_image_batch(digits)
            return images

        return generator

    class UniformTraining(GenericSGDLearner):
        def __init__(self, model, data_loader):
            super().__init__(model, data_loader)

        def loss_function(self, batch):
            probabilities = self.model(batch)
            uniform_probabilities = torch.tensor([[1.0 / number_of_digits] * number_of_digits] * len(batch))
            loss = torch.square(uniform_probabilities - probabilities).sum()
            return loss

    print("Uniform pre-training")
    print("Model:")
    print(recognizer)
    model = recognizer
    data_loader = data_loader_from_random_data_point_generator(number_of_batches_per_epoch,
                                                               random_batch_generator(),
                                                               print=None)
    UniformTraining(model, data_loader).learn()
    print("Uniform pre-training completed")
    print("Model:")
    print(recognizer)


main()
